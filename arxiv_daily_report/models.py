"""论文数据模型。"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field, asdict
from typing import Any
from zoneinfo import ZoneInfo

ARXIV_ABS = "https://arxiv.org/abs/{}"
ARXIV_PDF = "https://arxiv.org/pdf/{}"

_ID_RE = re.compile(r"(?:abs/|pdf/)?(?P<id>(?:\d{4}\.\d{4,5})|(?:[a-z\-]+(?:\.[A-Z]{2})?/\d{7}))v?(?P<ver>\d+)?$")


@dataclass
class Paper:
    """一篇 arXiv 论文（对应一个具体版本）。"""

    arxiv_id: str                      # 不含版本号，如 2610.18521
    version: int                       # 1 = 新投稿，>1 = 版本更新
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: str = ""
    published: str = ""                # v1 首次公开（UTC ISO8601）
    updated: str = ""                  # 本版本公开时刻（UTC ISO8601）
    primary_category: str = ""
    categories: list[str] = field(default_factory=list)
    comment: str = ""
    journal_ref: str = ""
    doi: str = ""
    # 运行期补充
    matched_categories: list[str] = field(default_factory=list)
    summary: str = ""                  # 模型生成的中文/英文要点
    themes: list[str] = field(default_factory=list)
    highlight: str = ""
    llm_model: str = ""
    llm_prompt_version: str = ""

    # -- 派生属性 ---------------------------------------------------------
    @property
    def uid(self) -> str:
        return f"{self.arxiv_id}v{self.version}"

    @property
    def is_new(self) -> bool:
        return self.version == 1

    @property
    def abs_url(self) -> str:
        return ARXIV_ABS.format(self.arxiv_id)

    @property
    def pdf_url(self) -> str:
        return ARXIV_PDF.format(self.arxiv_id)

    def dt_updated(self, tz: str = "UTC") -> dt.datetime:
        return _parse_iso(self.updated).astimezone(ZoneInfo(tz))

    def dt_published(self, tz: str = "UTC") -> dt.datetime:
        return _parse_iso(self.published).astimezone(ZoneInfo(tz))

    def day_str(self, tz: str) -> str:
        """本版本公开时刻在指定时区的自然日（YYYY-MM-DD）。"""
        return self.dt_updated(tz).strftime("%Y-%m-%d")

    def author_text(self, limit: int = 3) -> str:
        if not self.authors:
            return ""
        if len(self.authors) <= limit:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:limit]) + ", et al."

    def citation(self, index: int, author_limit: int = 3) -> str:
        """学术引用条目：作者、标题、arXiv 号、arXiv 链接。"""
        return f'[{index}] {self.author_text(author_limit)}, \u201c{self.title},\u201d arXiv:{self.arxiv_id}. {self.abs_url}'

    # -- 序列化 -----------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Paper":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


def _parse_iso(s: str) -> dt.datetime:
    if not s:
        return dt.datetime.now(dt.timezone.utc)
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        try:
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return dt.datetime.now(dt.timezone.utc)


def parse_arxiv_id(raw: str) -> tuple[str, int]:
    """从 abs URL 或裸 ID 解析 (id, version)。无法解析版本时返回 1。"""
    m = _ID_RE.search(raw.strip())
    if not m:
        return raw.strip(), 1
    return m.group("id"), int(m.group("ver") or 1)
