"""采集编排：把 arXiv 上的新论文抓进状态库，并决定每篇论文归属哪个订阅分类。"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .arxiv_client import ArxivClient, beijing_day_window, default_user_agent
from .config import Config
from .models import Paper
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class CollectResult:
    target_day: str
    window_start: str
    window_end: str
    per_category: dict[str, int] = field(default_factory=dict)   # 分类 -> 该分类当天新论文数
    new_papers: int = 0
    total_scanned: int = 0
    requests: int = 0
    papers: list[Paper] = field(default_factory=list)


def target_day(cfg: Config, now: dt.datetime | None = None) -> str:
    """按配置推导目标报告日（默认：运行时刻所在时区的前一天）。"""
    sched = cfg["schedule"]
    tz = ZoneInfo(sched.get("timezone", "Asia/Shanghai"))
    now = now or dt.datetime.now(tz)
    offset = int(sched.get("report_day_offset", 1) or 0)
    return (now.astimezone(tz).date() - dt.timedelta(days=offset)).isoformat()


def enabled_categories(cfg: Config) -> list[str]:
    out: list[str] = []
    for sub in cfg.get("subscriptions", []):
        c = (sub.get("category") or "").strip()
        if c and sub.get("enabled", True) and c not in out:
            out.append(c)
    return out


def assign(paper: Paper, subscribed: list[str], policy: str) -> dict[str, bool]:
    """返回 {订阅分类: 是否为该论文的归属报告}。"""
    matched = [c for c in subscribed if c in (paper.categories or [])]
    if not matched:
        return {}
    if policy == "all":
        return {c: True for c in matched}
    # primary / combined_dedup：优先主分类，其次按订阅顺序取第一个命中的
    if paper.primary_category in matched:
        chosen = paper.primary_category
    else:
        chosen = next((c for c in subscribed if c in matched), matched[0])
    return {c: (c == chosen) for c in matched}


def collect(
    cfg: Config,
    store: Store,
    *,
    day: str | None = None,
    lookback_days: int | None = None,
    only: list[str] | None = None,
    progress: Progress | None = None,
    client: ArxivClient | None = None,
) -> CollectResult:
    say: Progress = progress or (lambda m: log.info("%s", m))
    sched = cfg["schedule"]
    tz_name = sched.get("timezone", "Asia/Shanghai")
    day = day or target_day(cfg)
    lookback = int(lookback_days if lookback_days is not None else sched.get("catch_up_days", 7))
    lookback = max(1, lookback)

    subscribed = enabled_categories(cfg)
    if only:
        subscribed = [c for c in subscribed if c in only] or list(only)

    # 扫描窗口：目标日往前 lookback 天（覆盖周末/节假日延迟公告）
    end_dt = dt.datetime.fromisoformat(day + "T00:00:00").replace(tzinfo=ZoneInfo(tz_name))
    end_dt = (end_dt + dt.timedelta(days=1)).astimezone(dt.timezone.utc)
    start_dt = end_dt - dt.timedelta(days=lookback)

    fetch = cfg["fetch"]
    raw_dir = None
    if cfg["report"].get("keep_raw_xml", True):
        raw_dir = cfg.raw_cache_dir / day
    client = client or ArxivClient(
        interval_sec=fetch.get("request_interval_sec", 3.0),
        timeout_sec=fetch.get("timeout_sec", 60),
        page_size=fetch.get("page_size", 100),
        max_pages=fetch.get("max_pages", 60),
        user_agent=(fetch.get("user_agent") or default_user_agent(fetch.get("contact", ""))),
        retries=sched.get("retry", 3),
        raw_dir=raw_dir,
    )

    res = CollectResult(
        target_day=day,
        window_start=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    say(f"目标报告日：{day}（{tz_name}）  扫描窗口：{res.window_start} ~ {res.window_end}")

    seen: set[str] = set()
    for cat in subscribed:
        n_cat = 0
        for paper in client.iter_window(cat, start_dt, end_dt, tag=f"{cat}-{day}"):
            res.total_scanned += 1
            if not paper.is_new:
                continue                      # 只统计首次公开的 v1
            assignments = assign(paper, subscribed, cfg.get("crosslist_policy", "primary"))
            if not assignments:
                continue
            if store.add_paper(paper, assignments):
                res.new_papers += 1
                seen.add(paper.uid)
            if paper.day_str(tz_name) == day and assignments.get(cat):
                n_cat += 1
        res.per_category[cat] = n_cat
        say(f"  {cat:<22} 当日归属 {n_cat} 篇")
        for c in subscribed:
            res.per_category.setdefault(c, 0)

    res.requests = client.request_count
    res.papers = store.all_papers_on(tz_name, day)
    say(f"完成：新增入库 {res.new_papers} 条，目标日共 {len(res.papers)} 篇，API 请求 {res.requests} 次")
    return res


def list_papers(cfg: Config, store: Store, day: str, *, category: str | None = None) -> list[Paper]:
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")
    if category:
        return store.papers_for(category, tz_name, day, policy=cfg.get("crosslist_policy", "primary"))
    return store.all_papers_on(tz_name, day)
