"""PDF 渲染后端。

首选 WeasyPrint（真实 CID 字体子集嵌入 + CSS 分页：页码/页眉/书签/超链接）。
若 WeasyPrint 不可用则自动降级到 Chromium 无头打印（字体嵌入为 Type 3、无页码，
日志会明确告警），保证在任何环境下都能出 PDF。
"""
from __future__ import annotations

import html
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


class PdfError(RuntimeError):
    pass


def available_backends() -> list[str]:
    out: list[str] = []
    try:
        import weasyprint  # noqa: F401

        out.append("weasyprint")
    except Exception:
        pass
    if shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome"):
        out.append("chromium")
    return out


def render_pdf(
    html_text: str,
    out_path: Path,
    *,
    base_url: Path | None = None,
    backend: str = "auto",
    timeout_sec: int = 300,
) -> tuple[Path, str]:
    """把 HTML 渲染成 PDF，返回 (路径, 实际使用的后端)。"""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    order = [backend] if backend in ("weasyprint", "chromium") else available_backends()
    if not order:
        raise PdfError(
            "没有可用的 PDF 后端：请安装 WeasyPrint（推荐）或 Chromium。\n"
            "  pip install weasyprint    # Linux 还需 libpango/libcairo"
        )
    errors: list[str] = []
    for name in order:
        try:
            if name == "weasyprint":
                _weasyprint(html_text, out_path, base_url)
            else:
                _chromium(html_text, out_path, base_url, timeout_sec)
            return out_path, name
        except Exception as exc:  # noqa: BLE001
            log.warning("PDF 后端 %s 失败：%s", name, exc)
            errors.append(f"{name}: {exc}")
    raise PdfError("所有 PDF 后端均失败 -> " + "; ".join(errors))


def _weasyprint(html_text: str, out_path: Path, base_url: Path | None) -> None:
    import weasyprint

    kwargs = {}
    if base_url:
        kwargs["base_url"] = str(base_url)
    weasyprint.HTML(string=html_text, **kwargs).write_pdf(str(out_path))


def _chromium(html_text: str, out_path: Path, base_url: Path | None, timeout_sec: int) -> None:
    exe = shutil.which("chromium") or shutil.which("chromium-browser") or shutil.which("google-chrome")
    if not exe:
        raise PdfError("未找到 chromium 可执行文件")
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "report.html"
        src.write_text(html_text, encoding="utf-8")
        profile = Path(td) / "profile"
        profile.mkdir()
        cmd = [
            exe, "--headless=new", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", f"--user-data-dir={profile}",
            "--no-pdf-header-footer", "--virtual-time-budget=8000",
            f"--print-to-pdf={out_path}", src.as_uri(),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_sec)
        if proc.returncode != 0 or not out_path.exists():
            raise PdfError(f"chromium 退出码 {proc.returncode}: {(proc.stderr or '')[-200:]}")
        log.warning("已降级使用 Chromium 后端：字体嵌入为 Type 3 且无页码，建议安装 WeasyPrint")


def pdf_page_count(path: Path) -> int:
    try:
        proc = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True, timeout=30)
        for line in proc.stdout.splitlines():
            if line.startswith("Pages:"):
                return int(line.split(":")[1].strip())
    except Exception:
        pass
    return 0
