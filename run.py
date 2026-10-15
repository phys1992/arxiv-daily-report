#!/usr/bin/env python3
"""arxiv-daily-report 启动器。

直接运行：  python run.py daily
安装后：    arxiv-daily-report daily        （pipx install . 或 pip install .）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from arxiv_daily_report.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
