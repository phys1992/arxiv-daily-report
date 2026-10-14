"""日报摘要：把一天的论文交给模型，产出要点、主题综述与逐篇一句话。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from .llm.client import LLMClient, LLMError
from .models import Paper

log = logging.getLogger(__name__)

PROMPT_VERSION = "daily-v2"   # v2: 术语精准 / 逻辑严谨 / 语法从简

SYSTEM_ZH = """你是一位相对论与引力领域的科研助理，负责为 arXiv 每日新增论文撰写中文速览。

硬性规则：
1. 只能依据我提供的标题与摘要，不得引入任何外部知识或推测。
2. 不得编造论文、作者、结论或数值。
3. 逐篇要点 80–120 字，说明：研究了什么问题、用了什么方法、给出什么结论或预言。
   避免"本文研究了……"这类空话开头，直接进入内容。
4. 综述中若引用某篇论文，必须使用我给出的编号，例如 [3]，不得引用未提供的编号。
5. 全部输出为简体中文；专有名词、模型名、缩写保留英文原文。
6. 只输出 JSON，不要输出任何额外文字或 Markdown 代码块。

术语与表述：
7. 术语必须用领域标准译名，不得自造或混用。例如 quasinormal modes→准正规模，
   tidal deformability→潮汐形变率，equation of state→状态方程，ringdown→铃宕，
   inspiral→旋近，merger→并合，screening→屏蔽，backreaction→反作用，
   slow-roll→慢滚，extremal→极端，near-extremal→近极端，no-hair→无毛。
   拿不准的术语保留英文原词，不要勉强翻译。
8. 表述必须与原文一致。数值、量级、正负号、成立条件、适用区间不得改动，
   也不得简化到失真。原文没有给出的因果关系不要补。
9. 逻辑要完整：给结论时一并给出前提或适用范围；作对比时说明比较对象。
   无法判断的内容宁可不写，不要含糊其辞。
10. 语法从简：以短句为主，一句话只说一件事。少用长定语、嵌套从句和被动语态。
    不用比喻、夸张和口语化表达。同一件事只说一遍。"""

SYSTEM_EN = """You are a research assistant in relativity and gravitation, writing a concise
English digest of new arXiv papers.

Hard rules:
1. Use ONLY the titles and abstracts I provide. Do not add outside knowledge or speculation.
2. Never invent papers, authors, conclusions or numbers.
3. Per-paper note: 60-90 words covering the problem, the method, and the result or prediction.
4. When referring to a paper in the overview, use my numeric labels, e.g. [3], and only those.
5. Output plain English, keeping proper nouns and abbreviations as-is.
6. Output JSON only, with no extra text or Markdown fences.

Terminology and wording:
7. Use the standard technical term of the field; never invent or mix terms. If you are unsure of
   the standard term, keep the original English wording.
8. Stay faithful to the source. Do not alter or over-simplify numbers, magnitudes, signs,
   conditions of validity, or ranges. Do not add causal links the source does not state.
9. Be logically complete: state the premise or scope together with any conclusion, and name the
   comparison baseline when comparing. Omit what cannot be determined rather than being vague.
10. Keep the grammar simple: short sentences, one idea per sentence. Avoid long noun phrases,
    nested clauses and passive voice. No metaphors, no hype, no colloquialisms."""


@dataclass
class DaySummary:
    highlights: str = ""
    theme_notes: dict[str, str] = field(default_factory=dict)
    papers: dict[str, dict[str, Any]] = field(default_factory=dict)  # uid -> {summary, themes}
    model: str = ""
    prompt_version: str = PROMPT_VERSION
    ok: bool = False
    error: str = ""


def build_user_prompt(papers: list[Paper], category: str, day: str, taxonomy: list[dict], lang: str) -> str:
    theme_lines = "\n".join(
        f'- {t["code"]}: {t["zh"] if lang == "zh" else t["en"]}' for t in taxonomy
    )
    items = []
    for i, p in enumerate(papers, 1):
        items.append(
            {
                "n": i,
                "id": p.uid,
                "title": p.title,
                "authors": p.author_text(6),
                "primary": p.primary_category,
                "categories": p.categories,
                "abstract": p.abstract[:2200],
            }
        )
    header = (
        f"分类：{category}\n日期（北京时间）：{day}\n论文数量：{len(items)}\n\n"
        f"可选主题代码（每个论文只能从下列选 1–2 个）：\n{theme_lines}\n\n"
        f"论文列表（JSON）：\n{json.dumps(items, ensure_ascii=False)}"
    )
    if lang == "zh":
        tail = (
            "\n\n请输出如下 JSON：\n"
            '{"highlights":"3–5 条当日要点，每条 1–2 句，条与条之间用换行分隔，引用格式 [3]；'
            '若当日工作分散无明显主线，就概括整体趋势",'
            '"themes":{"主题代码":"该主题当日进展的 60–100 字综述，引用格式 [3]；没有进展的主题可省略"},'
            '"papers":[{"id":"论文的 id 字段原样","summary":"80–120 字中文要点","themes":["代码"]}]}'
            "\n注意：papers 数组必须覆盖全部论文，id 必须与我给出的完全一致。"
        )
    else:
        tail = (
            "\n\nReturn JSON:\n"
            '{"highlights":"3-5 bullets for the day, 1-2 sentences each, newline separated, cite as [3]",'
            '"themes":{"theme_code":"60-100 word overview of that theme today, cite as [3]"},'
            '"papers":[{"id":"exact id from input","summary":"60-90 word note","themes":["code"]}]}'
            "\nThe papers array must cover every paper, with ids exactly as given."
        )
    return header + tail


def summarize_day(
    client: LLMClient,
    papers: list[Paper],
    *,
    category: str,
    day: str,
    taxonomy: list[dict],
    lang: str = "zh",
) -> DaySummary:
    """调用模型生成当日摘要。失败时返回 ok=False，由调用方降级为纯元数据版。"""
    res = DaySummary(model=client.describe())
    if not papers:
        res.ok = True
        return res
    system = SYSTEM_ZH if lang == "zh" else SYSTEM_EN
    user = build_user_prompt(papers, category, day, taxonomy, lang)
    schema_hint = '{"highlights": "...", "themes": {"code": "..."}, "papers": [{"id": "...", "summary": "...", "themes": ["code"]}]}'
    # 自适应输出预算：每篇要点约 200 token（中文 80–120 字），外加要点与主题综述
    budget = max(2048, len(papers) * 260 + 1500)
    log.debug("摘要输出预算 %d tokens（%d 篇）", budget, len(papers))
    try:
        data = client.chat_json(system, user, schema_hint=schema_hint, max_tokens=budget)
    except LLMError as exc:
        res.error = str(exc)
        log.error("当日摘要失败：%s", exc)
        return res

    valid_codes = {t["code"] for t in taxonomy}
    by_uid = {p.uid: p for p in papers}
    by_index = {str(i): p.uid for i, p in enumerate(papers, 1)}

    res.highlights = str(data.get("highlights") or "").strip()
    for code, note in (data.get("themes") or {}).items():
        if code in valid_codes and str(note).strip():
            res.theme_notes[code] = str(note).strip()

    unknown = 0
    for item in data.get("papers") or []:
        if not isinstance(item, dict):
            continue
        raw_id = str(item.get("id") or "").strip()
        uid = raw_id if raw_id in by_uid else by_index.get(raw_id, "")
        if not uid:
            unknown += 1
            continue
        themes = [t for t in (item.get("themes") or []) if t in valid_codes][:2]
        res.papers[uid] = {
            "summary": str(item.get("summary") or "").strip(),
            "themes": themes or ["other"],
        }
    if unknown:
        log.warning("模型返回了 %d 条无法匹配的论文编号，已丢弃", unknown)
    missing = [p for p in papers if p.uid not in res.papers]
    if missing:
        log.warning("有 %d 篇论文没有拿到摘要（将保留元数据条目）", len(missing))
    res.ok = bool(res.papers) or bool(res.highlights)
    return res


def summarize_day_bilingual(
    client: LLMClient,
    papers: list[Paper],
    *,
    category: str,
    day: str,
    taxonomy: list[dict],
) -> DaySummary:
    """中英对照：中英各跑一遍，再按论文合并（仅在用户选择 zh-en 时使用，成本翻倍）。"""
    zh = summarize_day(client, papers, category=category, day=day, taxonomy=taxonomy, lang="zh")
    en = summarize_day(client, papers, category=category, day=day, taxonomy=taxonomy, lang="en")
    if zh.ok and not en.ok:
        return zh
    if en.ok and not zh.ok:
        return en
    if not (zh.ok or en.ok):
        return DaySummary(model=zh.model, ok=False, error=f"{zh.error} | {en.error}")

    merged = DaySummary(model=f"{zh.model}（中英对照）", ok=True)
    merged.highlights = _join_paragraphs(zh.highlights, en.highlights)
    codes = list(dict.fromkeys(list(zh.theme_notes) + list(en.theme_notes)))
    for code in codes:
        merged.theme_notes[code] = _join_paragraphs(
            zh.theme_notes.get(code, ""), en.theme_notes.get(code, "")
        )
    for p in papers:
        a = zh.papers.get(p.uid) or {}
        b = en.papers.get(p.uid) or {}
        summary = _join_paragraphs(a.get("summary", ""), b.get("summary", ""))
        themes = a.get("themes") or b.get("themes") or ["other"]
        merged.papers[p.uid] = {"summary": summary, "themes": themes}
    return merged


def _join_paragraphs(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    if a and b:
        return f"{a}\n{b}"
    return a or b


def apply_summary(papers: list[Paper], summary: DaySummary) -> None:
    for p in papers:
        item = summary.papers.get(p.uid)
        if not item:
            p.themes = p.themes or ["other"]
            continue
        p.summary = item.get("summary", "")
        p.themes = item.get("themes") or ["other"]
        p.llm_model = summary.model
        p.llm_prompt_version = summary.prompt_version


def backfill_summaries(
    client: LLMClient,
    store,
    papers: list[Paper],
    *,
    taxonomy: list[dict],
    lang: str = "zh",
    batch: int = 20,
    progress=None,
) -> int:
    """给缺少摘要的论文补生成（例如中途启用本工具、或某天日报失败）。

    这些论文的主题字段为空，会让月报把它们全归到「其他」，因此月报运行前补一次。
    """
    say = progress or (lambda m: log.info("%s", m))
    todo = [p for p in papers if not p.summary]
    if not todo:
        return 0
    done = 0
    total = len(todo)
    for i in range(0, total, batch):
        part = todo[i : i + batch]
        say(f"  补齐摘要：第 {i // batch + 1} 批（{len(part)} 篇，共 {total} 篇待补）")
        try:
            summary = summarize_day(
                client, part, category="(补齐)", day=part[0].day_str("Asia/Shanghai"),
                taxonomy=taxonomy, lang=lang,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("补齐摘要失败：%s", exc)
            continue
        if not summary.ok:
            continue
        apply_summary(part, summary)
        for p in part:
            if p.summary:
                store.save_paper_summary(p)
                done += 1
    say(f"  补齐完成：{done}/{total} 篇")
    return done
