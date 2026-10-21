"""常驻调度：到点自动抓取并生成报告；关机/停机后自动补齐。

systemd 只负责把本进程拉起来（Restart=always），定时逻辑全在这里，
因此 Web 界面改运行时间可以立即生效，无需重载任何服务。
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from zoneinfo import ZoneInfo

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

# 一轮失败后的重试间隔（秒）：5 分钟、15 分钟、30 分钟。
# 用完之后就放弃这一轮，等下一次定时任务——幂等 + 回溯窗口会把缺的那天补回来。
RETRY_DELAYS = (300, 900, 1800)
# 被 arXiv 限流时等得更久：对方明确说「请求太快」，5 分钟后就再撞一次没有意义。
RETRY_DELAYS_RATE_LIMIT = (1800, 3600)


def _delays_for(exc: Exception) -> tuple[int, ...]:
    """限流与普通故障用不同的重试节奏。"""
    from .arxiv_client import ArxivRateLimited

    return RETRY_DELAYS_RATE_LIMIT if isinstance(exc, ArxivRateLimited) else RETRY_DELAYS


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        hh, mm = (s or "19:00").split(":")[:2]
        return int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return 19, 0


def next_fire(cfg: Config, now: dt.datetime | None = None) -> dt.datetime:
    sched = cfg["schedule"]
    tz = ZoneInfo(sched.get("timezone", "Asia/Shanghai"))
    # 传进来的 now 可能带着别的时区（测试、或将来别的调用方），
    # 必须先换算；否则 now.replace(hour=19) 会落成"那个时区的 19:00"，
    # 在北京时间下会差 8 小时。
    now = (now or dt.datetime.now(tz)).astimezone(tz)
    if sched.get("mode") == "interval":
        minutes = max(5, int(sched.get("interval_minutes", 1440)))
        return now + dt.timedelta(minutes=minutes)
    hh, mm = _parse_hhmm(sched.get("time", "19:00"))
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return nxt


def run_once(cfg: Config, store: Store, *, day: str | None = None) -> None:
    """跑一轮：日报；若北京时间是每月 1 号，再补上一个自然月的简报。"""
    from .report_daily import run_daily

    run_daily(cfg, store, day=day)

    if day is None:
        from .report_monthly import run_monthly, should_run_monthly

        month = should_run_monthly(cfg)
        if month:
            log.info("今天是月初，生成 %s 月报", month)
            run_monthly(cfg, store, month=month)

        from .report_annual import run_annual, should_run_annual

        year = should_run_annual(cfg)
        if year:
            log.info("今天是元旦，生成 %s 年报", year)
            run_annual(cfg, store, year=year)


def run_daemon(cfg: Config, store: Store, *, with_web: bool = True) -> None:
    """常驻循环。启动时先补跑一次（幂等），之后按配置时刻触发。

    同时（默认）在后台线程里提供 Web 配置界面——systemd 用户服务跑的就是它，
    否则用服务方式常驻时就没有界面可用了。
    """
    if with_web and cfg["web"].get("enabled", True):
        from .server import start_web_thread

        start_web_thread(cfg, store)
    # 启动时清一次缓存，避免长期运行把磁盘撑满（只动缓存，不影响报告）
    try:
        from .cleanup import cleanup_cache

        cleanup_cache(cfg, store, progress=lambda m: log.info("%s", m))
    except Exception as exc:  # noqa: BLE001
        log.warning("缓存清理失败（不影响生成）：%s", exc)

    sched = cfg["schedule"]
    if sched.get("mode") == "manual":
        log.warning("调度模式为 manual：常驻进程不会自动运行，请通过 Web 界面或 CLI 手动触发")
    last_fire: str = store.kv_get("last_fire", "")
    log.info("调度器启动：模式=%s 时刻=%s 时区=%s",
             sched.get("mode"), sched.get("time"), sched.get("timezone"))

    # 启动即补跑：幂等 + 回溯窗口，能自动修复关机期间的空缺。
    # 失败也要安排重试——首次安装时 arXiv 限流是很常见的情况，
    # 不能因此干等到第二天的定时任务。
    retry_at: dt.datetime | None = None
    failures = 0
    if sched.get("mode") != "manual":
        try:
            log.info("启动补跑：生成最近一期日报")
            run_once(cfg, store)
            last_fire = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            store.kv_set("last_fire", last_fire)
        except Exception as exc:  # noqa: BLE001
            failures = 1
            delay = _delays_for(exc)[0]
            retry_at = dt.datetime.now(tz) + dt.timedelta(seconds=delay)
            log.warning("启动补跑失败（%s），%d 分钟后重试",
                        str(exc)[:200], delay // 60)
            log.debug("启动补跑的完整异常", exc_info=True)

    while True:
        try:
            cfg.load()                     # 支持运行中热更新配置
            tz = ZoneInfo(cfg["schedule"].get("timezone", "Asia/Shanghai"))
            now = dt.datetime.now(tz)
            target = retry_at if retry_at is not None else next_fire(cfg, now)
            wait = max(30.0, min((target - now).total_seconds(), 3600.0))
            if retry_at is not None:
                log.info("重试本轮：%.0f 秒后（之前失败 %d 次）", wait, failures)
            else:
                log.info("下次运行：%s（%.0f 秒后）", target.strftime("%Y-%m-%d %H:%M"), wait)
            time.sleep(wait)

            cfg.load()
            if cfg["schedule"].get("mode") == "manual":
                continue
            stamp = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
            log.info("触发日报生成：%s", stamp)
            run_once(cfg, store)
            store.kv_set("last_fire", stamp)
            retry_at, failures = None, 0
        except KeyboardInterrupt:
            log.info("调度器收到中断，退出")
            return
        except Exception as exc:  # noqa: BLE001
            failures += 1
            delays = (_delays_for(exc))
            if failures <= len(delays):
                delay = delays[failures - 1]
                retry_at = dt.datetime.now(tz) + dt.timedelta(seconds=delay)
                log.warning("本轮运行失败（第 %d 次）：%s；%d 分钟后重试",
                            failures, str(exc)[:200], delay // 60)
            else:
                log.error("已连续失败 %d 次，本轮放弃，等下一次定时任务"
                          "（回溯窗口会把缺的那天补上）", failures)
                retry_at, failures = None, 0
            log.debug("本轮失败的完整异常", exc_info=True)
