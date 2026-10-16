"""缓存清理：按配置的周期删掉可再生的中间数据，避免长期运行把磁盘撑满。

**只动缓存**，不碰状态与报告：
  * 缓存（可以随便删）：arXiv 原始 XML、公式 SVG 缓存、论文全文摘录；
  * 状态（删了会丢东西）：去重记录、日报摘要、用量账本 —— 放在 state 目录与
    数据库的主表里，这里绝不触碰；
  * 报告：用户最终要的文件，更不会动。

周期选项与 ``cache.cleanup`` 配置一一对应：1d / 3d / 1w / 1m / 3m / 6m / never。
默认 3m（三个月）。
"""
from __future__ import annotations

import datetime as dt
import logging
import re
import shutil

from .config import Config
from .store import Store

log = logging.getLogger(__name__)

__all__ = ["CLEANUP_OPTIONS", "DEFAULT_CLEANUP", "interval_days", "cleanup_cache",
           "cache_usage"]

# 选项 -> 天数（0 表示永不清理）
CLEANUP_OPTIONS: dict[str, int] = {
    "1d": 1,
    "3d": 3,
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "never": 0,
}
DEFAULT_CLEANUP = "3m"

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def interval_days(spec: str | None) -> int:
    """把配置里的周期字符串换成天数；不认识的值回落到默认。"""
    key = str(spec or "").strip().lower()
    return CLEANUP_OPTIONS.get(key, CLEANUP_OPTIONS[DEFAULT_CLEANUP])


def _older_than(path, cutoff: dt.date) -> bool:
    """按目录名（YYYY-MM-DD）判断是否过期；名字不合规就退回看修改时间。"""
    if _DATE_DIR.match(path.name):
        try:
            return dt.date.fromisoformat(path.name) < cutoff
        except ValueError:
            return False
    try:
        mtime = dt.date.fromtimestamp(path.stat().st_mtime)
    except OSError:
        return False
    return mtime < cutoff


def cache_usage(cfg: Config, store: Store | None = None) -> dict:
    """缓存占用的概览（供 status 展示）。"""
    out = {"raw_files": 0, "raw_bytes": 0, "math_rows": 0, "fulltext_rows": 0}
    raw = cfg.raw_cache_dir
    if raw.is_dir():
        for f in raw.rglob("*"):
            if f.is_file():
                out["raw_files"] += 1
                try:
                    out["raw_bytes"] += f.stat().st_size
                except OSError:
                    pass
    if store is not None:
        try:
            out["math_rows"] = store.conn.execute(
                "SELECT COUNT(*) FROM math_cache").fetchone()[0]
            out["fulltext_rows"] = store.conn.execute(
                "SELECT COUNT(*) FROM fulltext").fetchone()[0]
        except Exception:  # noqa: BLE001
            pass
    return out


def _human(n: int) -> str:
    v = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if v < 1024 or unit == "GB":
            return f"{v:.0f} {unit}" if unit in ("B", "KB") else f"{v:.1f} {unit}"
        v /= 1024
    return f"{v:.1f} GB"


def cleanup_cache(cfg: Config, store: Store | None = None, *, progress=None,
                  today: dt.date | None = None) -> dict:
    """按 ``cache.cleanup`` 清理缓存，返回统计。

    清理内容：
      1. 缓存目录下过期的 arXiv 原始 XML（按日期子目录判断）；
      2. 数据库 ``math_cache`` 里过期的公式 SVG；
      3. 数据库 ``fulltext`` 里过期的全文摘录。
    清理后如果确实删了东西，会 VACUUM 一次把文件真正缩小。
    """
    say = progress or (lambda m: log.info("%s", m))
    spec = str(cfg.get("cache", {}).get("cleanup", DEFAULT_CLEANUP))
    days = interval_days(spec)
    result = {"interval": spec, "days": days, "raw_removed": 0, "raw_bytes": 0,
              "math_removed": 0, "fulltext_removed": 0, "skipped": False}

    if days <= 0:
        result["skipped"] = True
        say("  缓存清理：已设置为不清理，跳过")
        return result

    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=days)
    result["cutoff"] = cutoff.isoformat()

    # 1) 原始 XML
    raw = cfg.raw_cache_dir
    if raw.is_dir():
        for child in sorted(raw.iterdir()):
            if not _older_than(child, cutoff):
                continue
            try:
                if child.is_dir():
                    size = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
                    n = sum(1 for f in child.rglob("*") if f.is_file())
                    shutil.rmtree(child)
                else:
                    size, n = child.stat().st_size, 1
                    child.unlink()
                result["raw_removed"] += n
                result["raw_bytes"] += size
            except OSError as exc:
                log.warning("清理缓存失败 %s：%s", child, exc)

    # 2) 数据库里的缓存表
    if store is not None:
        stamp = cutoff.isoformat()
        try:
            with store._lock:  # noqa: SLF001 - 同包内复用连接锁
                cur = store.conn.execute("DELETE FROM math_cache WHERE created_at < ?", (stamp,))
                result["math_removed"] = cur.rowcount or 0
                cur = store.conn.execute("DELETE FROM fulltext WHERE fetched_at < ?", (stamp,))
                result["fulltext_removed"] = cur.rowcount or 0
                store.conn.commit()
                if result["math_removed"] or result["fulltext_removed"]:
                    store.conn.execute("VACUUM")      # 让文件真的变小
        except Exception as exc:  # noqa: BLE001
            log.warning("清理数据库缓存失败：%s", exc)

    total = result["raw_removed"] + result["math_removed"] + result["fulltext_removed"]
    if total:
        say(f"  缓存清理（保留最近 {spec}）："
            f"原始 XML {result['raw_removed']} 个（{_human(result['raw_bytes'])}）、"
            f"公式 {result['math_removed']} 条、全文 {result['fulltext_removed']} 条")
    else:
        say(f"  缓存清理（保留最近 {spec}）：没有需要清理的内容")
    return result
