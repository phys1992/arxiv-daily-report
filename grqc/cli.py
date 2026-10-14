"""grqc-daily 命令行入口（也可通过 pipx install . 后直接调用 grqc-daily）。

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


def setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    # 第三方库过于啰嗦，只在出问题时才需要看
    for noisy in ("weasyprint", "fontTools", "PIL", "urllib3", "cssselect2", "tinycss2"):
        logging.getLogger(noisy).setLevel(logging.WARNING if not verbose else logging.INFO)


def get_ctx(args) -> tuple[Config, Store]:
    cfg = Config(path=Path(args.config).expanduser() if getattr(args, "config", None) else None)
    store = Store(cfg.data_dir / "state.sqlite3")
    return cfg, store


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="grqc-daily", description="arXiv 论文日报系统")
    ap.add_argument("-c", "--config", help="配置文件路径（默认 <项目根>/config.json）")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect", help="抓取论文入库（不生成报告）")
    p.add_argument("--day", help="目标报告日 YYYY-MM-DD（默认：昨天）")
    p.add_argument("--category", action="append", help="只抓指定分类（可重复）")
    p.add_argument("--lookback", type=int, help="回溯天数（默认取配置 catch_up_days）")

    p = sub.add_parser("daily", help="抓取并生成日报 PDF")
    p.add_argument("--day")
    p.add_argument("--no-llm", action="store_true", help="跳过模型摘要，只出元数据版")
    p.add_argument("--category", action="append")

    p = sub.add_parser("monthly", help="生成月报 PDF")
    p.add_argument("--month", help="YYYY-MM（默认：上个月）")

    p = sub.add_parser("serve", help="启动 Web 配置界面")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-open", action="store_true")

    sub.add_parser("daemon", help="常驻运行：定时抓取并生成报告")
    sub.add_parser("status", help="显示运行状态")

    args = ap.parse_args(argv)
    setup_logging(args.verbose)
    cfg, store = get_ctx(args)

    if args.cmd == "collect":
        from grqc.collect import collect

        res = collect(cfg, store, day=args.day, lookback_days=args.lookback, only=args.category)
        for cat, n in res.per_category.items():
            print(f"  {cat:<24} {n:>4} 篇")
        print(f"目标日 {res.target_day}：入库新增 {res.new_papers}，当日论文 {len(res.papers)}")
        return 0

    if args.cmd == "daily":
        from grqc.report_daily import run_daily

        paths = run_daily(cfg, store, day=args.day, only=args.category, use_llm=not args.no_llm)
        for p in paths:
            print(f"  生成：{p}")
        return 0

    if args.cmd == "monthly":
        from grqc.report_monthly import run_monthly

        paths = run_monthly(cfg, store, month=args.month)
        for p in paths:
            print(f"  生成：{p}")
        return 0

    if args.cmd == "serve":
        from grqc.server import serve

        serve(cfg, store, host=args.host, port=args.port, open_browser=not args.no_open)
        return 0

    if args.cmd == "daemon":
        from grqc.scheduler import run_daemon

        run_daemon(cfg, store)
        return 0

    if args.cmd == "status":
        from grqc.status import render_status

        print(render_status(cfg, store))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
