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


# ---------------------------------------------------------------------------
# 限流冷却：被 arXiv 说 "Rate exceeded" 之后，别在惩罚期里继续重试。
# 条款明确禁止"attempt to circumvent rate limits"，持续试探只会延长封锁。
#
# 实测（2026-10-19）：停手后约 44 秒即可恢复，限流是速率限制而非定时封禁。
# 因此起步只要 5 分钟；若仍失败再逐级加长，封顶 30 分钟。
# 任何一次请求成功后就归零，下次重新从 5 分钟开始。
# ---------------------------------------------------------------------------
COOLDOWN_KEY = "arxiv_cooldown_until"
COOLDOWN_COUNT_KEY = "arxiv_cooldown_count"
COOLDOWN_STEPS = (300, 600, 1200, 1800)      # 5 / 10 / 20 / 30 分钟（封顶）


def cooldown_until(store: Store) -> dt.datetime | None:
    raw = store.kv_get(COOLDOWN_KEY, "")
    if not raw:
        return None
    try:
        return dt.datetime.fromisoformat(raw)
    except ValueError:
        return None


def note_rate_limited(store: Store, reset: bool = False) -> dt.datetime:
    """记一次限流，返回解禁时刻（逐次加长）。"""
    level = 0 if reset else int(store.kv_get(COOLDOWN_COUNT_KEY, "0") or 0)
    step = COOLDOWN_STEPS[min(level, len(COOLDOWN_STEPS) - 1)]
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=step)
    store.kv_set(COOLDOWN_KEY, until.isoformat())
    store.kv_set(COOLDOWN_COUNT_KEY, str(level + 1))
    return until


def clear_cooldown(store: Store) -> None:
    if store.kv_get(COOLDOWN_KEY, ""):
        store.kv_set(COOLDOWN_KEY, "")
        store.kv_set(COOLDOWN_COUNT_KEY, "0")


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
        # 任意一次请求成功 -> 冷却归零，下次限流重新从 5 分钟起
        on_success=lambda: clear_cooldown(store),
    )

    res = CollectResult(
        target_day=day,
        window_start=start_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        window_end=end_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
    say(f"目标报告日：{day}（{tz_name}）  扫描窗口：{res.window_start} ~ {res.window_end}")

    # 惩罚期内不再请求，直接说明情况
    until = cooldown_until(store)
    if until is not None and dt.datetime.now(dt.timezone.utc) < until:
        left = int((until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60) + 1
        from .arxiv_client import ArxivRateLimited

        raise ArxivRateLimited(
            f"arXiv 正在限制本机请求（Rate exceeded），冷却中，约 {left} 分钟后恢复。"
            "期间不会再请求 arXiv，以免延长封锁。"
        )

    from .arxiv_client import ArxivRateLimited as _RateLimited

    # 已经抓过的旧日期不再重复请求：重跑历史日报时不该再打 arXiv。
    # （定时任务跑的是「昨天」，距今 1 天，不受影响；只有 ≥2 天前才跳过。）
    try:
        today = dt.datetime.now(ZoneInfo(tz_name)).date()
        age_days = (today - dt.date.fromisoformat(day)).days
    except Exception:  # noqa: BLE001
        age_days = 0
    if age_days >= 2 and not only:
        existing = store.all_papers_on(tz_name, day)
        if existing:
            say(f"  {day} 的论文已入库（{len(existing)} 篇），跳过网络请求")
            counts: dict[str, int] = {c: 0 for c in subscribed}
            for p in existing:
                if p.primary_category in counts:
                    counts[p.primary_category] += 1
            res.per_category = counts
            res.papers = existing
            res.requests = 0
            return res

    seen: set[str] = set()
    try:
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
    except _RateLimited:
        # arXiv 说"Rate exceeded"：记下解禁时刻，期间不再请求
        until = note_rate_limited(store)
        left = max(1, int((until - dt.datetime.now(dt.timezone.utc)).total_seconds() // 60))
        say(f"  arXiv 限流：已进入 {left} 分钟冷却（到 {until:%H:%M}Z），期间不再请求")
        raise
    else:
        clear_cooldown(store)

    res.requests = client.request_count
    res.papers = store.all_papers_on(tz_name, day)
    say(f"完成：新增入库 {res.new_papers} 条，目标日共 {len(res.papers)} 篇，API 请求 {res.requests} 次")
    return res


def list_papers(cfg: Config, store: Store, day: str, *, category: str | None = None) -> list[Paper]:
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")
    if category:
        return store.papers_for(category, tz_name, day, policy=cfg.get("crosslist_policy", "primary"))
    return store.all_papers_on(tz_name, day)
