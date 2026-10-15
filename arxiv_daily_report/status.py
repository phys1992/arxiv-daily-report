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


def _web_hint(cfg: Config) -> str:
    """把「怎么打开 Web 界面」直接写在状态里，省得去翻日志。"""
    from .server import web_url

    host = cfg["web"].get("host", "127.0.0.1")
    port = int(cfg["web"].get("port", 8765))
    if not cfg["web"].get("enabled", True):
        return "已在配置中关闭（web.enabled=false）"
    try:
        url = web_url(cfg, host, port)
    except Exception:  # noqa: BLE001
        return f"http://{host}:{port}/（令牌读取失败）"
    if host in ("0.0.0.0", "::"):
        return url + "  ← 注意：正在对外监听，端口 " + str(port)
    return url


def render_status(cfg: Config, store: Store) -> str:
    s = status_dict(cfg, store)
    lines = [
        "arxiv-daily-report 运行状态",
        "=" * 46,
        f"  模式            {'便携（' + s['paths']['portable'] + '）' if s['paths']['portable'] else '系统安装（XDG 目录）'}",
        f"  配置目录        {s['paths']['config']}",
        f"  数据目录        {s['paths']['data']}",
        f"  缓存目录        {s['paths']['cache']}",
        f"  报告目录        {s['paths']['reports']}",
        f"  发布目录        {s['paths']['publish'] or '（未设置）'}",
        f"  PDF 后端        {', '.join(s['pdf_backends']) or '（无！请安装 WeasyPrint）'}",
        f"  配置界面        {_web_hint(cfg)}",
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
