"""arxiv-daily-report 命令行入口（也可通过 pipx install . 后直接调用 arxiv-daily-report）。

  python run.py collect [--day YYYY-MM-DD] [--category gr-qc]
  python run.py daily   [--day YYYY-MM-DD] [--no-llm]
  python run.py monthly [--month YYYY-MM]
  python run.py serve   [--port 8765]
  python run.py daemon
  python run.py status
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config  # noqa: E402
from .store import Store  # noqa: E402


def setup_logging(verbose: bool, tz: str | None = None) -> None:
    """日志时间用配置里的时区，而不是进程的本地时区。

    机器本身可能是 UTC（容器/服务器常见），日志与 status 若显示 UTC，
    会和报告上写的"北京时间"对不上，排查时容易误判。
    """
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(tz or "Asia/Shanghai")

    class _LocalTimeFormatter(logging.Formatter):
        def formatTime(self, record, datefmt=None):  # noqa: N802
            import datetime as _dt

            when = _dt.datetime.fromtimestamp(record.created, zone)
            return when.strftime(datefmt or "%Y-%m-%d %H:%M:%S")

    handler = logging.StreamHandler()
    handler.setFormatter(_LocalTimeFormatter("%(asctime)s %(levelname)-7s %(message)s",
                                             "%H:%M:%S"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    # 第三方库过于啰嗦，只在出问题时才需要看
    for noisy in ("weasyprint", "fontTools", "PIL", "urllib3", "cssselect2", "tinycss2"):
        logging.getLogger(noisy).setLevel(logging.WARNING if not verbose else logging.INFO)


def get_ctx(args) -> tuple[Config, Store]:
    cfg = Config(path=Path(args.config).expanduser() if getattr(args, "config", None) else None)
    store = Store(cfg.data_dir / "state.sqlite3")
    return cfg, store


def print_welcome(cfg: Config, store: Store, *, prog: str = "arxiv-daily-report") -> None:
    """不带子命令时打印用法与状态。

    直接报 "the following arguments are required: cmd" 对使用者没有任何帮助，
    这里先回答「我能做什么、界面怎么进」，再给当前状态。
    """
    from .server import token_required, web_url
    from .status import render_status

    print(f"{prog} — arXiv 论文日报")
    print()
    print("用法")
    print(f"  {prog} serve          启动 Web 配置界面（推荐从这里开始）")
    print(f"  {prog} daily          抓取并生成昨天的日报 PDF")
    print(f"  {prog} monthly        生成上一个自然月的简报")
    print(f"  {prog} daemon         常驻定时运行（同时提供配置界面）")
    print(f"  {prog} status         查看运行状态")
    print(f"  {prog} <命令> --help  查看某个命令的参数")
    print()

    if not cfg["web"].get("enabled", True):
        print("配置界面已在配置中关闭（web.enabled=false）。")
    else:
        try:
            url = web_url(cfg)
        except Exception:  # noqa: BLE001
            url = ""
        if url:
            print(f"配置界面：{url}")
            if token_required(cfg):
                print("  已开启令牌校验，请用上面这个完整地址访问。")
            else:
                print(f"  免令牌，直接在浏览器打开即可；还没启动就运行 `{prog} serve`。")

    if not cfg.api_key(cfg["llm"].get("provider", "deepseek")):
        print()
        print(f"提示：还没配置模型密钥。启动配置界面（{prog} serve）后在「大模型设置」里填写，")
        print("      或先设环境变量，例如 export DEEPSEEK_API_KEY=sk-...")

    print()
    print("当前状态")
    print(render_status(cfg, store))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="arxiv-daily-report",
        description="arXiv 论文日报：抓取新增论文，用大模型生成 PDF 日报与月度简报。",
        epilog="不带子命令时显示当前状态与用法。",
    )
    ap.add_argument("-c", "--config", help="配置文件路径（默认 <项目根>/config.json）")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=False, metavar="命令")

    p = sub.add_parser("collect", help="抓取论文入库（不生成报告）")
    p.add_argument("--day", help="目标报告日 YYYY-MM-DD（默认：昨天）")
    p.add_argument("--category", action="append", help="只抓指定分类（可重复）")
    p.add_argument("--lookback", type=int, help="回溯天数（默认取配置 catch_up_days）")

    p = sub.add_parser("daily", help="抓取并生成日报 PDF")
    p.add_argument("--day")
    p.add_argument("--no-llm", action="store_true", help="跳过模型摘要，只出元数据版")
    p.add_argument("--category", action="append")
    p.add_argument("--no-monthly", action="store_true",
                   help="即使今天是月初也不自动生成上月月报")

    p = sub.add_parser("monthly", help="生成月报 PDF（依据该月的日报）")
    p.add_argument("--month", help="YYYY-MM（默认：上一个自然月）")
    p.add_argument("--no-llm", action="store_true", help="只出统计版简报")

    p = sub.add_parser("annual", help="生成年报 PDF（按子主题，参考日报与月报）")
    p.add_argument("--year", help="YYYY（默认：上一个自然年）")
    p.add_argument("--no-llm", action="store_true", help="只出统计版报告")

    p = sub.add_parser("serve", help="启动 Web 配置界面")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-open", action="store_true")

    pd = sub.add_parser("daemon", help="常驻运行：定时抓取并生成报告（同时提供 Web 配置界面）")
    pd.add_argument("--no-web", action="store_true", help="不提供 Web 配置界面")
    sub.add_parser("status", help="显示运行状态")
    pc = sub.add_parser("cleanup", help="清理缓存（可再生的中间数据）")
    pc.add_argument("--dry-run", action="store_true", help="只显示会清理什么，不真的删")

    args = ap.parse_args(argv)
    # 时区要从配置里读，所以先建 Config 再配日志
    try:
        _cfg_early = Config(path=Path(args.config).expanduser()
                            if getattr(args, "config", None) else None)
        _tz = _cfg_early["schedule"].get("timezone", "Asia/Shanghai")
    except Exception:  # noqa: BLE001 - 配置坏了也要能打出日志
        _tz = "Asia/Shanghai"
    setup_logging(args.verbose, _tz)
    cfg, store = get_ctx(args)

    try:
        return _dispatch(args, cfg, store, ap)
    except Exception as exc:  # noqa: BLE001 - 统一转成可读提示，不要甩 traceback
        return _report_failure(exc)


def _report_failure(exc: Exception) -> int:
    """把常见故障翻译成用户能照做的提示，而不是 Python traceback。"""
    from .arxiv_client import ArxivError, ArxivRateLimited

    if isinstance(exc, ArxivRateLimited):
        print(f"  arXiv 限流：{exc}", file=sys.stderr)
        print("  arXiv 对 API 请求有严格限制（每 3 秒最多 1 次），这是临时状态，不是故障。", file=sys.stderr)
        print("  等几分钟再执行一次即可：arxiv-daily-report daily", file=sys.stderr)
        print("  若反复出现，可以把 fetch.request_interval_sec 调大（默认 3 秒），", file=sys.stderr)
        print("  或把 schedule.catch_up_days 调小（默认 7 天）。", file=sys.stderr)
        return 3
    if isinstance(exc, ArxivError):
        print(f"  arXiv 请求失败：{exc}", file=sys.stderr)
        print("  检查网络连通性与 arXiv 服务状态后重试。", file=sys.stderr)
        return 3
    print(f"  执行失败：{exc}", file=sys.stderr)
    if args_verbose_hint():
        raise exc
    print("  加 -v 可以看到完整堆栈。", file=sys.stderr)
    return 1


def args_verbose_hint() -> bool:
    import logging

    return logging.getLogger().isEnabledFor(logging.DEBUG)


def _dispatch(args, cfg, store, ap) -> int:
    if not args.cmd:
        print_welcome(cfg, store, prog=ap.prog)
        return 0

    if args.cmd == "collect":
        from arxiv_daily_report.collect import collect

        res = collect(cfg, store, day=args.day, lookback_days=args.lookback, only=args.category)
        for cat, n in res.per_category.items():
            print(f"  {cat:<24} {n:>4} 篇")
        print(f"目标日 {res.target_day}：入库新增 {res.new_papers}，当日论文 {len(res.papers)}")
        return 0

    if args.cmd == "daily":
        from arxiv_daily_report.report_daily import run_daily

        paths = run_daily(cfg, store, day=args.day, only=args.category, use_llm=not args.no_llm)
        for p in paths:
            print(f"  生成：{p}")
        # 北京时间每月 1 号：顺带把上一个自然月的简报也出了
        if not args.no_monthly and args.day is None:
            from arxiv_daily_report.report_monthly import run_monthly, should_run_monthly

            m = should_run_monthly(cfg)
            if m:
                print(f"  今天是月初，自动生成 {m} 月报")
                for p in run_monthly(cfg, store, month=m, use_llm=not args.no_llm):
                    print(f"  生成：{p}")
        return 0

    if args.cmd == "monthly":
        from arxiv_daily_report.report_monthly import run_monthly

        paths = run_monthly(cfg, store, month=args.month, use_llm=not args.no_llm)
        for p in paths:
            print(f"  生成：{p}")
        return 0

    if args.cmd == "annual":
        from arxiv_daily_report.report_annual import run_annual

        paths = run_annual(cfg, store, year=args.year, use_llm=not args.no_llm)
        for p in paths:
            print(f"  生成：{p}")
        return 0

    if args.cmd == "serve":
        from arxiv_daily_report.server import serve

        serve(cfg, store, host=args.host, port=args.port, open_browser=not args.no_open)
        return 0

    if args.cmd == "daemon":
        from arxiv_daily_report.scheduler import run_daemon

        run_daemon(cfg, store, with_web=not getattr(args, "no_web", False))
        return 0

    if args.cmd == "cleanup":
        from arxiv_daily_report.cleanup import cache_usage, cleanup_cache, interval_days

        before = cache_usage(cfg, store)
        print(f"缓存目录：{cfg.cache_home}")
        print(f"  当前：原始 XML {before['raw_files']} 个、公式 {before['math_rows']} 条、"
              f"全文 {before['fulltext_rows']} 条")
        days = interval_days(cfg.get("cache", {}).get("cleanup"))
        if args.dry_run:
            print(f"  保留周期：{days} 天（--dry-run，未做任何删除）" if days else "  已设置为不清理")
            return 0
        cleanup_cache(cfg, store, progress=print)
        after = cache_usage(cfg, store)
        print(f"  清理后：原始 XML {after['raw_files']} 个、公式 {after['math_rows']} 条、"
              f"全文 {after['fulltext_rows']} 条")
        return 0

    if args.cmd == "status":
        from arxiv_daily_report.status import render_status

        print(render_status(cfg, store))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
