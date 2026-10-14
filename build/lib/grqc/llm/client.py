"""多厂商大模型客户端。

统一接口 chat_json()：所有厂商都归一化成「输入消息 -> 结构化 dict」。
两个线上协议适配器：
  * openai    —— DeepSeek / Kimi / OpenAI / 本地兼容服务
  * anthropic —— Claude（Messages API：system 独立字段、无 response_format、
                 响应在 content[0].text、鉴权用 x-api-key）
"""
from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..config import Config
from ..store import Store
from .registry import Provider, get as get_provider

log = logging.getLogger(__name__)

ANTHROPIC_VERSION = "2023-06-01"


class LLMError(RuntimeError):
    pass


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0


class LLMClient:
    def __init__(self, cfg: Config, store: Store | None = None, provider_key: str | None = None):
        self.cfg = cfg
        self.store = store
        llm = cfg["llm"]
        self.provider: Provider = get_provider(provider_key or llm.get("provider", "deepseek"))
        self.base_url = (llm.get("base_url") or "").strip() or self.provider.base_url
        if provider_key and provider_key != llm.get("provider"):
            self.base_url = self.provider.base_url
        self.model = (llm.get("model") or "").strip() or self.provider.default_model
        self.temperature = float(llm.get("temperature", 0) or 0)
        self.max_tokens = int(llm.get("max_tokens", 4096) or 4096)
        self.timeout = int(llm.get("timeout_sec", 180) or 180)
        self.api_key = cfg.api_key(self.provider.key)
        self.last_usage = Usage()
        self.calls = 0
        self.last_finish_reason = ""

    # ------------------------------------------------------------------
    @property
    def ready(self) -> bool:
        if self.provider.key == "custom":
            return bool(self.base_url)          # 本地服务常无需密钥
        return bool(self.api_key)

    def describe(self) -> str:
        return f"{self.provider.label} / {self.model}"

    # ------------------------------------------------------------------
    def list_models(self) -> list[str]:
        """拉取模型列表（Web 界面「获取模型列表」按钮）。"""
        url = self.base_url.rstrip("/") + self.provider.models_path
        headers = {"Accept": "application/json"}
        if self.api_key:
            if self.provider.wire == "anthropic":
                headers["x-api-key"] = self.api_key
                headers["anthropic-version"] = ANTHROPIC_VERSION
            else:
                headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=min(self.timeout, 60)) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        items = data.get("data") or data.get("models") or []
        out = []
        for it in items:
            mid = it.get("id") or it.get("name")
            if mid:
                out.append(mid)
        return sorted(out)

    def test_connection(self) -> dict[str, Any]:
        t0 = time.time()
        try:
            models = self.list_models()
            return {
                "ok": True,
                "latency_ms": int((time.time() - t0) * 1000),
                "models": models[:50],
                "message": f"连接正常，返回 {len(models)} 个模型",
            }
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                    "message": f"HTTP {exc.code} {exc.reason} {body}"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "latency_ms": int((time.time() - t0) * 1000),
                    "message": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    def chat_json(
        self, system: str, user: str, *, schema_hint: str = "", retries: int = 3,
        max_tokens: int | None = None,
    ) -> dict:
        """返回解析后的 JSON 对象。失败抛 LLMError。

        max_tokens 可临时覆盖（例如按论文篇数自适应预算）。
        """
        if not self.ready:
            raise LLMError(f"{self.provider.label} 未配置 API Key")
        original = self.max_tokens
        if max_tokens:
            self.max_tokens = max(256, int(max_tokens))
        last: Exception | None = None
        try:
            for attempt in range(1, max(1, retries) + 1):
                try:
                    text = self._call(system, user, schema_hint)
                    obj = _parse_json(text)
                    if obj is None:
                        if self.last_finish_reason == "length":
                            raise LLMError(
                                f"输出被 max_tokens({self.max_tokens}) 截断，JSON 不完整"
                                f"（已收到 {len(text)} 字符）"
                            )
                        raise LLMError(f"模型输出不是合法 JSON：{text[:200]}")
                    return obj
                except Exception as exc:  # noqa: BLE001
                    last = exc
                    log.warning("模型调用失败（第 %d 次）：%s", attempt, exc)
                    # 输出被截断时自动放宽预算再试（长列表常见）
                    if "截断" in str(exc) and self.max_tokens < 32000:
                        self.max_tokens = min(32000, int(self.max_tokens * 2))
                        log.info("输出被截断，预算提高到 %d tokens 后重试", self.max_tokens)
                    elif attempt < retries:
                        time.sleep(min(30.0, 2.0 ** attempt * 2))
            raise LLMError(f"模型调用连续失败：{last}")
        finally:
            self.max_tokens = original

    def _call(self, system: str, user: str, schema_hint: str) -> str:
        if self.provider.wire == "anthropic":
            return self._call_anthropic(system, user, schema_hint)
        return self._call_openai(system, user)

    def _call_openai(self, system: str, user: str) -> str:
        url = self.base_url.rstrip("/") + "/chat/completions"
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.provider.supports_json_mode:
            body["response_format"] = {"type": "json_object"}
        # DeepSeek 默认开启思考模式：推理 token 会挤占 max_tokens，导致正文为空。
        # 结构化摘要场景显式关闭思考模式，更快也更省 token。
        if self.provider.key == "deepseek" and self.cfg["llm"].get("disable_thinking", True):
            body["thinking"] = {"type": "disabled"}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = self._post(url, body, headers)
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError(f"响应结构异常：{str(data)[:200]}") from exc
        u = data.get("usage") or {}
        self.last_usage = Usage(int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)))
        self.last_finish_reason = str(choice.get("finish_reason") or "")
        self._log_usage()
        if not (text or "").strip():
            finish = choice.get("finish_reason")
            reasoning = (choice.get("message") or {}).get("reasoning_content") or ""
            if finish == "length":
                raise LLMError(
                    f"输出被 max_tokens({self.max_tokens}) 截断，正文为空"
                    f"（推理占用 {len(reasoning)} 字符）。请提高 llm.max_tokens 或关闭思考模式。"
                )
            raise LLMError(f"模型返回空正文（finish_reason={finish}）")
        return text

    def _call_anthropic(self, system: str, user: str, schema_hint: str) -> str:
        url = self.base_url.rstrip("/") + "/messages"
        sys_text = system
        if schema_hint:
            sys_text += "\n\n只输出一个 JSON 对象，不要输出任何解释文字或 Markdown 代码块。字段要求：\n" + schema_hint
        body = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "system": sys_text,
            "messages": [{"role": "user", "content": user}],
        }
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        }
        data = self._post(url, body, headers)
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if isinstance(b, dict))
        u = data.get("usage") or {}
        self.last_usage = Usage(int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)))
        self._log_usage()
        return text

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8", errors="replace"))
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            raise LLMError(f"HTTP {exc.code} {exc.reason} {detail}") from exc

    def _log_usage(self) -> None:
        self.calls += 1
        if self.store:
            self.store.add_usage(
                self.provider.key, self.model,
                self.last_usage.prompt_tokens, self.last_usage.completion_tokens, 1,
            )


def _parse_json(text: str) -> dict | None:
    """容错解析：直接解析 -> 剥离代码围栏 -> 截取最外层花括号。"""
    if not text:
        return None
    s = text.strip()
    for candidate in (s, _strip_fence(s), _outer_braces(s)):
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None


def _strip_fence(s: str) -> str:
    m = re.search(r"```(?:json)?\s*(.*?)```", s, re.S)
    return m.group(1).strip() if m else ""


def _outer_braces(s: str) -> str:
    i, j = s.find("{"), s.rfind("}")
    return s[i : j + 1] if 0 <= i < j else ""
