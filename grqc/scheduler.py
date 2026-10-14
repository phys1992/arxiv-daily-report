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


def _parse_hhmm(s: str) -> tuple[int, int]:
    try:
        hh, mm = (s or "19:00").split(":")[:2]
        return int(hh), int(mm)
    except Exception:  # noqa: BLE001
        return 19, 0


def next_fire(cfg: Config, now: dt.datetime | None = None) -> dt.datetime:
    sched = cfg["schedule"]
    tz = ZoneInfo(sched.get("timezone", "Asia/Shanghai"))
    now = now or dt.datetime.now(tz)
    if sched.get("mode") == "interval":
        minutes = max(5, int(sched.get("interval_minutes", 1440)))
        return now + dt.timedelta(minutes=minutes)
    hh, mm = _parse_hhmm(sched.get("time", "19:00"))
    nxt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if nxt <= now:
        nxt += dt.timedelta(days=1)
    return nxt


def run_once(cfg: Config, store: Store, *, day: str | None = None) -> None:
    from .report_daily import run_daily

    run_daily(cfg, store, day=day)


def run_daemon(cfg: Config, store: Store) -> None:
    """常驻循环。启动时先补跑一次（幂等），之后按配置时刻触发。"""
    sched = cfg["schedule"]
    if sched.get("mode") == "manual":
        log.warning("调度模式为 manual：常驻进程不会自动运行，请通过 Web 界面或 CLI 手动触发")
    last_fire: str = store.kv_get("last_fire", "")
    log.info("调度器启动：模式=%s 时刻=%s 时区=%s",
             sched.get("mode"), sched.get("time"), sched.get("timezone"))

    # 启动即补跑：幂等 + 回溯窗口，能自动修复关机期间的空缺
    if sched.get("mode") != "manual":
        try:
            log.info("启动补跑：生成最近一期日报")
            run_once(cfg, store)
            last_fire = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            store.kv_set("last_fire", last_fire)
        except Exception as exc:  # noqa: BLE001
            log.exception("启动补跑失败：%s", exc)

    while True:
        try:
            cfg.load()                     # 支持运行中热更新配置
            tz = ZoneInfo(cfg["schedule"].get("timezone", "Asia/Shanghai"))
            now = dt.datetime.now(tz)
            nxt = next_fire(cfg, now)
            wait = max(30.0, min((nxt - now).total_seconds(), 3600.0))
            log.info("下次运行：%s（%.0f 秒后）", nxt.strftime("%Y-%m-%d %H:%M"), wait)
            time.sleep(wait)

            cfg.load()
            if cfg["schedule"].get("mode") == "manual":
                continue
            stamp = dt.datetime.now(tz).strftime("%Y-%m-%d %H:%M")
            log.info("触发日报生成：%s", stamp)
            run_once(cfg, store)
            store.kv_set("last_fire", stamp)
        except KeyboardInterrupt:
            log.info("调度器收到中断，退出")
            return
        except Exception as exc:  # noqa: BLE001
            log.exception("本轮运行失败，%s 后重试", "5 分钟")
            time.sleep(300)
