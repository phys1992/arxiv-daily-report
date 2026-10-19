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
    """解析 arXiv 的时间戳，**总是**返回带时区的 datetime。

    arXiv API 的 ``published`` / ``updated`` 是 UTC（形如 ``2026-10-16T17:30:38Z``），
    但也要能容忍别的写法：带显式偏移的照用，**不带时区的一律按 UTC 解释**。
    这一点很关键——naive datetime 交给 ``astimezone()`` 会按本机时区解释，
    在北京时间的机器上就会整体偏移 8 小时，把论文算到错误的那一天。
    """
    if not s:
        return dt.datetime.now(dt.timezone.utc)
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        pass
    try:
        value = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        log.warning("无法解析时间戳 %r，按当前时间处理", s)
        return dt.datetime.now(dt.timezone.utc)
    if value.tzinfo is None:                 # 没写时区 -> 按 arXiv 的约定当成 UTC
        value = value.replace(tzinfo=dt.timezone.utc)
    return value


def day_bounds_utc(day: str | dt.date, tz: str = "Asia/Shanghai") -> tuple[dt.datetime, dt.datetime]:
    """某时区的自然日 -> UTC 半开区间 ``[start, end)``。

    报告日、抓取窗口、按日查询都走这个函数，时区只在换算这一步参与，
    其他地方一律用 UTC 比较，避免「同一时刻在不同地方被算成不同天」。
    """
    from zoneinfo import ZoneInfo

    d = dt.date.fromisoformat(day) if isinstance(day, str) else day
    zone = ZoneInfo(tz)
    start = dt.datetime(d.year, d.month, d.day, tzinfo=zone).astimezone(dt.timezone.utc)
    return start, start + dt.timedelta(days=1)


def month_bounds_utc(month: str, tz: str = "Asia/Shanghai") -> tuple[dt.datetime, dt.datetime]:
    """某时区的自然月（YYYY-MM）-> UTC 半开区间 ``[start, end)``。"""
    from zoneinfo import ZoneInfo

    year, mon = (int(x) for x in str(month).split("-")[:2])
    zone = ZoneInfo(tz)
    start = dt.datetime(year, mon, 1, tzinfo=zone).astimezone(dt.timezone.utc)
    nxt = dt.datetime(year + (mon // 12), (mon % 12) + 1, 1, tzinfo=zone)
    return start, nxt.astimezone(dt.timezone.utc)


def day_bounds_epoch(day: str | dt.date, tz: str = "Asia/Shanghai") -> tuple[int, int]:
    """某时区自然日 -> UTC 的 (起始秒, 结束秒)，供数据库按整数比较。"""
    start, end = day_bounds_utc(day, tz)
    return int(start.timestamp()), int(end.timestamp())


def month_bounds_epoch(month: str, tz: str = "Asia/Shanghai") -> tuple[int, int]:
    start, end = month_bounds_utc(month, tz)
    return int(start.timestamp()), int(end.timestamp())


def parse_arxiv_id(raw: str) -> tuple[str, int]:
    """从 abs URL 或裸 ID 解析 (id, version)。无法解析版本时返回 1。"""
    m = _ID_RE.search(raw.strip())
    if not m:
        return raw.strip(), 1
    return m.group("id"), int(m.group("ver") or 1)
