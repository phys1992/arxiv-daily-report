"""双模型：一个负责生成，另一个负责校验，然后生成模型据意见修订一次。

为什么值得这么做：摘要类产物最容易出的问题不是"写得不好"，而是**写偏了**——
把摘要里没有的结论说成论文结论、把条件丢掉、把数值记错、把两篇论文的工作混在
一起。这类错误生成模型自己很难发现，但换一个模型对照原始材料逐条核对就很容易
看出来。

约定：
  * 校验模型**只挑错，不改写**，并且必须指出依据（原文哪一句、哪篇论文）；
  * 生成模型拿到意见后**修订一次**，输出仍按原来的 JSON 结构，便于复用解析代码；
  * 任何一步失败都不影响主流程——拿不到修订结果就沿用初版，只记一条告警。
"""
from __future__ import annotations

import json
import logging
from typing import Callable

from .config import Config
from .llm.client import LLMClient, LLMError
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

__all__ = ["verify_client", "enabled", "describe", "review_and_revise"]

CHECK_SCHEMA = (
    '{"issues":[{"where":"问题出现在哪一段/哪一篇（简要定位）",'
    '"problem":"具体问题（事实错误/条件缺失/数值不符/无中生有/表述不清）",'
    '"evidence":"依据：原文或原始材料里的哪句话",'
    '"fix":"应当怎么改"}],'
    '"verdict":"ok 或 revise"}'
)


def _llm_conf(cfg: Config) -> dict:
    return (cfg.get("llm", {}) or {}).get("verify", {}) or {}


def enabled(cfg: Config) -> bool:
    conf = _llm_conf(cfg)
    return bool(conf.get("enabled")) and bool(str(conf.get("model") or "").strip())


def describe(cfg: Config) -> str:
    """给日志/状态用的一行描述。"""
    if not enabled(cfg):
        return ""
    conf = _llm_conf(cfg)
    provider = str(conf.get("provider") or "").strip() or str(
        (cfg.get("llm", {}) or {}).get("provider") or "")
    model = str(conf.get("model") or "").strip()
    return f"{provider or '同生成模型'} / {model}"


def verify_client(cfg: Config, store: Store | None = None) -> LLMClient | None:
    """按配置构造校验模型；未启用或没填模型时返回 None。"""
    if not enabled(cfg):
        return None
    conf = _llm_conf(cfg)
    provider = str(conf.get("provider") or "").strip() or None
    base_url = str(conf.get("base_url") or "").strip() or None
    client = LLMClient(cfg, store, provider_key=provider,
                       model=str(conf.get("model") or "").strip(),
                       base_url=base_url,
                       api_key=cfg.verify_api_key() or None,
                       role="校验")
    if not client.ready:
        log.warning("校验模型 %s 没有可用的密钥，本次不做校验", client.describe())
        return None
    return client


def _system(subject: str) -> str:
    return (
        f"你是一位极其严格的审稿人，负责核对一份{subject}是否忠实于原始材料。\n"
        "你的任务是**挑错**，不是润色。只报告确有依据的问题：\n"
        "1. 无中生有：材料里没有的结论、数值、机制，被写进了产物；\n"
        "2. 事实错误：数值、符号、因果方向、适用范围与材料不符；\n"
        "3. 条件缺失：省略了成立条件，导致结论看起来比材料里更强；\n"
        "4. 张冠李戴：把 A 论文的结果安到 B 论文头上；\n"
        "5. 引用错误：引用的论文编号或日期与材料对不上。\n"
        "不要因为风格、详略、用词偏好提出问题；不要提出改写建议。"
        "每条问题都必须给出材料中的依据原句。若确实没有问题，verdict 填 ok。"
    )


def review_and_revise(
    generator: LLMClient,
    verifier: LLMClient,
    *,
    draft: dict,
    source: str,
    subject: str,
    checklist: str,
    system: str,
    output_hint: str,
    max_tokens: int = 4096,
    progress: Progress | None = None,
) -> tuple[dict | None, dict]:
    """校验 → 修订一次。返回 (修订后的对象, 校验结论)。修订失败时修订结果为 None。"""
    say: Progress = progress or (lambda m: log.info("%s", m))

    # ---- 1) 校验 ----
    user = (
        f"原始材料：\n{source}\n\n"
        f"待核对的{subject}（JSON）：\n{json.dumps(draft, ensure_ascii=False)}\n\n"
        f"核对要点：\n{checklist}\n\n"
        f"请输出 JSON：{CHECK_SCHEMA}"
    )
    try:
        verdict = verifier.chat_json(_system(subject), user,
                                     schema_hint=CHECK_SCHEMA, max_tokens=4096)
    except LLMError as exc:
        say(f"  校验模型调用失败，沿用初版：{exc}")
        return None, {}

    issues = [it for it in (verdict.get("issues") or []) if isinstance(it, dict)]
    if str(verdict.get("verdict") or "").lower() == "ok" and not issues:
        say("  校验通过，无需修订")
        return None, verdict
    if not issues:
        say("  校验未提出具体问题，沿用初版")
        return None, verdict

    say(f"  校验提出 {len(issues)} 条意见，交由生成模型修订")

    # ---- 2) 修订 ----
    lines = []
    for i, it in enumerate(issues, 1):
        lines.append(
            f"{i}. [{it.get('problem', '')}] {it.get('where', '')}\n"
            f"   依据：{it.get('evidence', '')}\n"
            f"   建议：{it.get('fix', '')}"
        )
    user2 = (
        f"原始材料：\n{source}\n\n"
        f"这是你的初版（JSON）：\n{json.dumps(draft, ensure_ascii=False)}\n\n"
        f"审稿人提出了以下问题，请**逐条处理**后输出修订版：\n" + "\n".join(lines) + "\n\n"
        "要求：只修正确有问题的地方，未涉及的条目保持原样；不要新增材料里没有的内容；"
        "仍然输出同样的 JSON 结构，不要附加任何解释。\n"
        f"请输出 JSON：{output_hint}"
    )
    try:
        revised = generator.chat_json(system, user2,
                                      schema_hint=output_hint, max_tokens=max_tokens)
    except LLMError as exc:
        say(f"  修订调用失败，沿用初版：{exc}")
        return None, verdict
    return revised, verdict
