"""缓存清理：按配置的周期删掉可再生的中间数据，避免长期运行把磁盘撑满。

**只动缓存**，不碰状态与报告：
  * 缓存（可以随便删）：arXiv 原始 XML、公式 SVG 缓存、论文全文摘录；
  * 状态（删了会丢东西）：去重记录、日报摘要、用量账本 —— 放在 state 目录与
    数据库的主表里，这里绝不触碰；
  * 报告：用户最终要的文件，更不会动。

周期选项与 ``cache.cleanup`` 配置一一对应：
1w / 2w / 1m / 2m / 3m / 6m / 1y / never，默认 3m（三个月）。
最短一周——缓存本来就不必天天清，周期太短只会让重复抓取变多。
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
           "cache_usage", "release_abstracts"]

# 选项 -> 天数（0 表示永不清理）。最短一周。
CLEANUP_OPTIONS: dict[str, int] = {
    "1w": 7,        # 每周
    "2w": 14,       # 每 2 周
    "1m": 30,       # 每月
    "2m": 60,       # 每 2 个月
    "3m": 90,       # 每 3 个月（默认）
    "6m": 180,      # 每半年
    "1y": 365,      # 每年
    "never": 0,     # 永久保留
}
DEFAULT_CLEANUP = "3m"

# 旧版本用过、现在已下线的最小周期；落到新的最短周期而不是跳到默认值，
# 免得升级后行为突然从「每天清」变成「三个月清」。
_LEGACY = {"1d": "1w", "3d": "1w"}

_DATE_DIR = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def interval_days(spec: str | None) -> int:
    """把配置里的周期字符串换成天数。

    未知值回落到默认（3 个月）；旧版用过的 1d/3d 按新的最短周期 1w 处理，
    避免升级后清理频率意外大幅放宽。
    """
    key = str(spec or "").strip().lower()
    key = _LEGACY.get(key, key)
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
    out = {"raw_files": 0, "raw_bytes": 0, "math_rows": 0, "fulltext_rows": 0,
           "db_bytes": 0, "papers": 0, "papers_with_abstract": 0, "abstract_bytes": 0}
    db = cfg.data_dir / "state.sqlite3"
    try:
        out["db_bytes"] = db.stat().st_size
    except OSError:
        pass
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
            import json as _json
            for (payload,) in store.conn.execute("SELECT payload FROM papers"):
                out["papers"] += 1
                try:
                    ab = _json.loads(payload).get("abstract") or ""
                except Exception:  # noqa: BLE001
                    ab = ""
                if ab:
                    out["papers_with_abstract"] += 1
                    out["abstract_bytes"] += len(ab.encode())
        except Exception:  # noqa: BLE001
            pass
    return out


def release_abstracts(cfg: Config, store: Store, *, cutoff: dt.date | None = None,
                      progress=None, today: dt.date | None = None) -> dict:
    """释放「已经用不上」的论文摘要原文 —— 它占了库容的七成。

    为什么可以释放：``abstract`` 只在**生成要点**时用得到（渲染 PDF 并不需要它），
    而且随时能从 arXiv API 重新取回。所以只要满足下面两条，它就等于缓存：

      1. 该论文已经有**当前提示词版本**的要点 —— 重跑这一天会走复用路径，
         不会再把它送进模型；
      2. 它所属日期的日报摘要有存档 —— 保证那次重跑确实能复用。

    不满足条件的（要点还是旧版本、当天没跑过、从未总结过）一律保留原文，
    以免下次总结时没有输入。只改 payload 里的 abstract 字段，其他字段原样保留。
    """
    from .summarize import PROMPT_VERSION

    say = progress or (lambda m: log.info("%s", m))
    today = today or dt.date.today()
    if cutoff is None:
        days = interval_days(str(cfg.get("cache", {}).get("cleanup", DEFAULT_CLEANUP)))
        cutoff = today - dt.timedelta(days=days) if days > 0 else today
    result = {"released": 0, "bytes": 0, "cutoff": cutoff.isoformat()}

    # 先找出「整天都满足复用条件」的 (日期, 分类)：只有这些天重跑时才会走复用路径，
    # 不会把论文再送进模型，摘要原文才是真的用不上了。
    from .summarize import PROMPT_VERSION as PV

    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    policy = cfg.get("crosslist_policy", "primary")
    safe_days: set[tuple[str, str]] = set()
    for row in store.conn.execute("SELECT day, category FROM day_digest"):
        day, cat = row["day"], row["category"]
        if day >= cutoff.isoformat():
            continue
        try:
            papers = store.papers_for(cat, tz, day, policy=policy)
        except Exception:  # noqa: BLE001
            continue
        if papers and all((p.summary or "").strip() and p.llm_prompt_version == PV
                          for p in papers):
            safe_days.add((day, cat))
    result["safe_days"] = len(safe_days)

    import json as _json
    updates: list[tuple[str, str]] = []
    if safe_days:
        # 用与生成报告完全相同的取数逻辑（按配置时区的自然日），
        # 这样「哪些论文属于这一天」的口径不会和报告不一致
        # 与 papers_for 完全相同的口径：分类 + 该时区自然日的 UTC 区间
        from .models import day_bounds_epoch

        rows, seen_uids = [], set()
        for day, cat in sorted(safe_days):
            start, end = day_bounds_epoch(day, tz)
            sql = (
                "SELECT DISTINCT p.uid, p.payload FROM papers p "
                "JOIN paper_categories c ON c.uid=p.uid "
                "WHERE c.category=? AND p.ts_utc>=? AND p.ts_utc<?"
            )
            if policy != "all":
                sql += " AND c.is_primary=1"
            for row in store.conn.execute(sql, (cat, start, end)):
                if row["uid"] not in seen_uids:
                    seen_uids.add(row["uid"])
                    rows.append(row)
        for r in rows:
            try:
                d = _json.loads(r["payload"])
            except Exception:  # noqa: BLE001
                continue
            ab = d.get("abstract") or ""
            if not ab:
                continue
            if (d.get("summary") or "").strip() and d.get("llm_prompt_version") == PV:
                del d["abstract"]
                updates.append((_json.dumps(d, ensure_ascii=False), r["uid"]))
                result["released"] += 1
                result["bytes"] += len(ab.encode())
    if updates:
        with store._lock:  # noqa: SLF001 - 同包内复用连接锁
            store.conn.executemany("UPDATE papers SET payload=? WHERE uid=?", updates)
            store.conn.commit()
            store.conn.execute("VACUUM")          # 真正把文件缩回去
    if result["released"]:
        say(f"  释放摘要原文：{result['released']} 篇（{_human(result['bytes'])}）"
            f"—— 这些论文已有当前版本要点，原文可从 arXiv 重新获取")
    return result


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
              "math_removed": 0, "fulltext_removed": 0, "abstracts_released": 0,
              "abstract_bytes": 0, "skipped": False}

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

    # 3) 论文摘要原文（占了库容七成，但要满足安全条件才释放）
    if store is not None and not result.get("skipped"):
        try:
            rel = release_abstracts(cfg, store, cutoff=cutoff,
                                    progress=lambda m: log.debug("%s", m))
            result["abstracts_released"] = rel["released"]
            result["abstract_bytes"] = rel["bytes"]
        except Exception as exc:  # noqa: BLE001
            log.warning("释放摘要原文失败：%s", exc)

    total = (result["raw_removed"] + result["math_removed"]
             + result["fulltext_removed"] + result.get("abstracts_released", 0))
    if total:
        say(f"  缓存清理（保留最近 {spec}）："
            f"原始 XML {result['raw_removed']} 个（{_human(result['raw_bytes'])}）、"
            f"公式 {result['math_removed']} 条、全文 {result['fulltext_removed']} 条、"
            f"论文摘要原文 {result.get('abstracts_released', 0)} 篇"
            f"（{_human(result.get('abstract_bytes', 0))}）")
    else:
        say(f"  缓存清理（保留最近 {spec}）：没有需要清理的内容")
    return result
