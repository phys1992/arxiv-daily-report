"""运行状态汇总（CLI status 与 Web 状态页共用）。"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from .collect import enabled_categories, target_day
from .config import Config
from .store import Store


def _usage_cost(prompt_tokens: int, completion_tokens: int, provider: str) -> float:
    """粗略估算费用（人民币）。DeepSeek 空闲时段价：输入 1 元/M，输出 4 元/M。"""
    if provider in ("deepseek", "custom"):
        return prompt_tokens / 1_000_000 * 1.0 + completion_tokens / 1_000_000 * 4.0
    return prompt_tokens / 1_000_000 * 2.0 + completion_tokens / 1_000_000 * 8.0


def status_dict(cfg: Config, store: Store) -> dict:
    today = dt.date.today()
    day = target_day(cfg)
    month_start = today.replace(day=1).isoformat() + "T00:00:00Z"
    usage = store.usage_since(month_start)
    papers_today = store.all_papers_on(cfg["schedule"].get("timezone", "Asia/Shanghai"), day)
    reports = store.list_reports("daily", limit=12)
    runs = store.recent_runs(8)

    cats = enabled_categories(cfg)
    return {
        "target_day": day,
        "categories": cats,
        "category_count": len(cats),
        "papers_today": len(papers_today),
        "papers_total": int(store.conn.execute("SELECT COUNT(*) c FROM papers").fetchone()["c"]),
        "math_cache": store.math_count(),
        "usage": {
            "calls": usage["calls"],
            "prompt_tokens": usage["prompt_tokens"],
            "completion_tokens": usage["completion_tokens"],
            "cost_cny": round(
                _usage_cost(usage["prompt_tokens"], usage["completion_tokens"],
                            cfg["llm"].get("provider", "deepseek")), 3),
        },
        "next_run": next_run_time(cfg),
        "reports": reports,
        "runs": runs,
        "paths": {
            "config": str(cfg.config_home),
            "data": str(cfg.data_dir),
            "cache": str(cfg.cache_dir),
            "reports": str(cfg.reports_dir),
            "publish": str(cfg.publish_dir) if cfg.publish_dir else "",
            "portable": str(cfg.portable) if cfg.portable else "",
        },
        "pdf_backends": _backends(),
    }


def _backends() -> list[str]:
    try:
        from .pdf import available_backends

        return available_backends()
    except Exception:  # noqa: BLE001
        return []


def next_run_time(cfg: Config) -> str:
    sched = cfg["schedule"]
    if sched.get("mode") == "manual":
        return "（仅手动）"
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(sched.get("timezone", "Asia/Shanghai"))
        hh, mm = (sched.get("time", "19:00") or "19:00").split(":")
        now = dt.datetime.now(tz)
        nxt = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        if nxt <= now:
            nxt += dt.timedelta(days=1)
        delta = nxt - now
        hours = int(delta.total_seconds() // 3600)
        mins = int(delta.total_seconds() % 3600 // 60)
        return f"{nxt.strftime('%Y-%m-%d %H:%M')} ({tz.key}，约 {hours} 小时 {mins} 分后)"
    except Exception:  # noqa: BLE001
        return sched.get("time", "")


def _cache_hint(cfg: Config, store: Store | None = None) -> str:
    """缓存占用与清理周期（让用户心里有数）。"""
    from .cleanup import DEFAULT_CLEANUP, cache_usage, interval_days

    spec = str(cfg.get("cache", {}).get("cleanup", DEFAULT_CLEANUP))
    days = interval_days(spec)
    try:
        u = cache_usage(cfg, store)
        size = u["raw_bytes"]
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                human = f"{size:.0f} {unit}" if unit in ("B", "KB") else f"{size:.1f} {unit}"
                break
            size /= 1024
        used = f"原始 XML {u['raw_files']} 个（{human}）"
    except Exception:  # noqa: BLE001
        used = "?"
    when = "不清理" if days <= 0 else f"每 {days} 天（{spec}）"
    return f"{used}，{when}"


def _storage_hint(cfg: Config, store: Store | None = None) -> str:
    """库容与增长来源，便于判断长期运行会不会撑爆。"""
    from .cleanup import cache_usage

    def human(n: float) -> str:
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024 or unit == "GB":
                return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} GB"

    try:
        u = cache_usage(cfg, store)
    except Exception:  # noqa: BLE001
        return "?"
    return (f"数据库 {human(u['db_bytes'])}（{u['papers']} 篇，"
            f"其中 {u['papers_with_abstract']} 篇仍存摘要原文 {human(u['abstract_bytes'])}）"
            f"，缓存 {human(u['raw_bytes'])}")


def _math_hint(cfg: Config, store: Store | None = None) -> str:
    """实际会用哪个公式后端——装完环境第一件想确认的事。"""
    try:
        from .mathrender import _matplotlib_ready, _set_mplconfigdir

        conf = cfg.get("report", {}).get("math_backend", "auto")
        node = __import__("shutil").which("node")
        script = cfg.assets_dir / "mathjax" / "render.js"
        modules = cfg.assets_dir / "mathjax" / "node_modules" / "mathjax-full"
        mj = bool(node) and script.exists() and modules.exists() and conf != "unicode"

        mpl = False
        if conf != "unicode":
            _set_mplconfigdir()
            mpl = _matplotlib_ready()

        if conf == "unicode":
            return "Unicode 近似（配置强制指定，观感较差）"
        if conf in ("auto", "mathjax") and mj:
            return "MathJax（最佳）"
        if conf == "auto" and mpl:
            return "matplotlib mathtext（Computer Modern 字形）"
        if conf == "matplotlib" and mpl:
            return "matplotlib mathtext（Computer Modern 字形）"
        return ("Unicode 近似（未装 matplotlib，公式无法排版；"
                "pip install --user matplotlib 可解决）")
    except Exception as exc:  # noqa: BLE001
        return f"无法判断（{exc}）"


def _arxiv_hint(cfg: Config, store: Store | None = None) -> str:
    """请求节奏 + 当前是否处于限流冷却期。"""
    try:
        from .arxiv_client import API_MIN_INTERVAL, CRAWL_DELAY
        from .collect import cooldown_until

        api = cfg["fetch"].get("request_interval_sec", 5.0)
        ft = cfg.get("fulltext", {}).get("throttle_seconds", 20.0)
        base = (f"API 每 {max(API_MIN_INTERVAL, float(api)):.0f} 秒 1 次 · "
                f"全文每 {max(CRAWL_DELAY, float(ft)):.0f} 秒 1 篇")
        if store is None:
            return base
        until = cooldown_until(store)
        if until is None:
            return base + " · 未限流"
        now = dt.datetime.now(dt.timezone.utc)
        if until <= now:
            return base + " · 冷却已结束"
        left = int((until - now).total_seconds() // 60) + 1
        return base + f" · 限流冷却中，约 {left} 分钟后解除（{until:%H:%M}Z）"
    except Exception as exc:  # noqa: BLE001
        return f"无法读取（{exc}）"


def _fonts() -> dict:
    """当前预设实际会用到的字体（用于状态展示）。"""
    from .render import resolved_fonts

    try:
        return resolved_fonts(Config())
    except Exception:  # noqa: BLE001
        return {"latin": "?", "cjk": "?"}


def _web_hint(cfg: Config) -> str:
    """把「怎么打开 Web 界面」直接写在状态里，省得去翻日志。"""
    from .server import web_url

    host = cfg["web"].get("host", "127.0.0.1")
    port = int(cfg["web"].get("port", 8765))
    if not cfg["web"].get("enabled", True) and cfg["schedule"].get("mode") != "manual":
        return "常驻服务已关闭界面（web.enabled=false）"
    from .server import token_required

    try:
        url = web_url(cfg, host, port)
    except Exception:  # noqa: BLE001
        return f"http://{host}:{port}/（令牌读取失败）"
    tail = "" if token_required(cfg) else "  （无需令牌，直接打开）"
    if host in ("0.0.0.0", "::"):
        tail = "  ← 正在对外监听，需要密码"
    return url + tail


def render_status(cfg: Config, store: Store) -> str:
    s = status_dict(cfg, store)
    lines = [
        "arxiv-daily-report 运行状态",
        "=" * 46,
        f"  模式            {'便携（' + s['paths']['portable'] + '）' if s['paths']['portable'] else '系统安装（XDG 目录）'}",
        f"  配置目录        {s['paths']['config']}",
        f"  状态目录        {s['paths']['data']}",
        f"  缓存目录        {s['paths']['cache']}",
        f"  报告目录        {s['paths']['reports']}",
        f"  发布目录        {s['paths']['publish'] or '（未设置）'}",
        f"  PDF 后端        {', '.join(s['pdf_backends']) or '（无！请安装 WeasyPrint）'}",
        f"  字体            西文 {_fonts()['latin']}",
        f"                  中文 {_fonts()['cjk']}",
        f"  配置界面        {_web_hint(cfg)}",
        f"  缓存清理        {_cache_hint(cfg, store)}",
        f"  arXiv 请求      {_arxiv_hint(cfg, store)}",
        f"  公式排版        {_math_hint(cfg, store)}",
        f"  存储占用        {_storage_hint(cfg, store)}",
        "",
        f"  订阅分类        {s['category_count']} 个：{', '.join(s['categories'])}",
        f"  目标报告日      {s['target_day']}",
        f"  当日论文        {s['papers_today']} 篇（库内累计 {s['papers_total']}）",
        f"  公式缓存        {s['math_cache']} 条",
        f"  下次运行        {s['next_run']}",
        "",
        f"  本月模型调用    {s['usage']['calls']} 次，"
        f"输入 {s['usage']['prompt_tokens']} / 输出 {s['usage']['completion_tokens']} tokens，"
        f"约 ¥{s['usage']['cost_cny']}",
        "",
        "  最近报告：",
    ]
    for r in s["reports"][:8]:
        lines.append(f"    {r['day']}  {r['category']:<16} {r['count']:>4} 篇  v{r['revision']}")
    lines.append("")
    lines.append("  最近运行：")
    for r in s["runs"][:6]:
        lines.append(f"    {r['started_at']}  {r['status']:<8} {(r['detail'] or '')[:60]}")
    return "\n".join(lines)
