"""把生成好的 PDF 复制到用户设置的「发布目录」（例如 Seafile / 坚果云同步盘）。

发布目录不是必需项：留空就什么都不做。它的意义是让同步盘里直接有一个
可读的目录树，不用去翻 `~/.local/share` 里的报告根目录。

相对路径按 `paths.reports` 保持原样，这样三类产物互不覆盖：

    <reports>/gr-qc/2026-10/2026-10-20.pdf      -> <publish>/gr-qc/2026-10/2026-10-20.pdf
    <reports>/monthly/gr-qc/2026-10.pdf         -> <publish>/monthly/gr-qc/2026-10.pdf
    <reports>/annual/gr-qc/2026.pdf             -> <publish>/annual/gr-qc/2026.pdf
"""
from __future__ import annotations

import logging
from pathlib import Path

from .config import Config

log = logging.getLogger(__name__)

__all__ = ["publish_to", "write_latest"]


def publish_to(cfg: Config, pdf_path: Path) -> Path | None:
    """复制到发布目录；返回目标路径，未设置或失败时返回 None。"""
    dest_root = cfg.publish_dir
    if not dest_root:
        return None
    try:
        rel = pdf_path.relative_to(cfg.reports_dir)
    except ValueError:
        # 报告不在报告根目录下（例如用户自定义了 out_dir），退回只按文件名
        rel = Path(pdf_path.name)
    dest = dest_root / rel
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(pdf_path.read_bytes())
    except OSError as exc:
        # 同步盘掉线、权限不对都不该让整期报告作废
        log.warning("发布到 %s 失败（报告本身已生成）：%s", dest, exc)
        return None
    log.info("已发布到 %s", dest)
    return dest


def write_latest(out_dir: Path, pdf_path: Path) -> Path | None:
    """在 out_dir 里留一份 latest.pdf，便于反复打开同一路径。"""
    latest = out_dir / "latest.pdf"
    try:
        latest.write_bytes(pdf_path.read_bytes())
    except OSError as exc:
        log.warning("写入 %s 失败：%s", latest, exc)
        return None
    return latest
