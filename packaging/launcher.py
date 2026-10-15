#!/usr/bin/python3
"""arxiv-daily-report 命令入口（发行版安装包使用）。

发行版把 Python 包装在 /usr/lib/arxiv-daily-report/ 下（而不是各发行版
python3.X/site-packages 里），这样同一个包可以在 Debian/Fedora/Arch 上通用，
不必为每个发行版的 Python 版本目录分别打包。

数据文件在 /usr/share/arxiv-daily-report/data/，由包内代码自动发现。
"""
import sys

sys.path.insert(0, "/usr/lib/arxiv-daily-report")

try:
    from arxiv_daily_report.cli import main
except ImportError as exc:  # pragma: no cover - 仅在包损坏时触发
    sys.stderr.write(
        "无法导入 arxiv_daily_report：%s\n"
        "请确认已安装本包（dpkg -l arxiv-daily-report / rpm -q arxiv-daily-report）。\n" % exc
    )
    raise SystemExit(1)

if __name__ == "__main__":
    raise SystemExit(main())
