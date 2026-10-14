"""大模型厂商预设表。

wire 表示线上协议：
  * openai    —— OpenAI Chat Completions 兼容（DeepSeek / Kimi / OpenAI / 本地服务）
  * anthropic —— Anthropic Messages（Claude 原生协议，与 OpenAI 不同）
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    base_url: str
    default_model: str
    wire: str
    env_var: str
    supports_json_mode: bool = True
    models_path: str = "/models"
    note: str = ""


PROVIDERS: dict[str, Provider] = {
    "deepseek": Provider(
        key="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com/v1",
        default_model="deepseek-flash",
        wire="openai",
        env_var="DEEPSEEK_API_KEY",
        note="默认厂商。空闲时段（北京时间工作日 9-12、14-18 之外）价格减半。",
    ),
    "moonshot": Provider(
        key="moonshot",
        label="Kimi / Moonshot",
        base_url="https://api.moonshot.cn/v1",
        default_model="moonshot-v1-32k",
        wire="openai",
        env_var="MOONSHOT_API_KEY",
    ),
    "openai": Provider(
        key="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        wire="openai",
        env_var="OPENAI_API_KEY",
    ),
    "anthropic": Provider(
        key="anthropic",
        label="Claude / Anthropic",
        base_url="https://api.anthropic.com/v1",
        default_model="claude-sonnet-4-5",
        wire="anthropic",
        env_var="ANTHROPIC_API_KEY",
        supports_json_mode=False,   # Messages API 无 response_format，靠提示词 + 容错解析
        note="Claude 使用 Anthropic Messages 协议（非 OpenAI 格式），系统提示为独立字段。",
    ),
    "custom": Provider(
        key="custom",
        label="自定义（OpenAI 兼容）",
        base_url="http://127.0.0.1:11434/v1",
        default_model="",
        wire="openai",
        env_var="CUSTOM_API_KEY",
        note="可填本地部署的 OpenAI 兼容服务，例如 Ollama / vLLM / LM Studio。",
    ),
}

ORDER = ["deepseek", "moonshot", "openai", "anthropic", "custom"]


def get(key: str) -> Provider:
    return PROVIDERS.get((key or "").strip().lower()) or PROVIDERS["deepseek"]


def describe_all() -> list[dict]:
    return [
        {
            "key": p.key,
            "label": p.label,
            "base_url": p.base_url,
            "default_model": p.default_model,
            "env_var": p.env_var,
            "note": p.note,
        }
        for p in (PROVIDERS[k] for k in ORDER)
    ]
