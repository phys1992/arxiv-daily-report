"""HTML 报告生成（再由 PDF 后端渲染成 PDF）。

版式要点：
  * @font-face 直接引用仓库内的思源黑体，不依赖系统是否安装中文字体；
  * CSS 分页媒体提供页码、页眉与 PDF 书签（bookmark-level）；
  * 参考文献由代码生成（作者、标题、arXiv 号、链接），模型不参与。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .i18n import rt
from .models import Paper
from .mathrender import MathService

log = logging.getLogger(__name__)

PROMPT_VERSION = "daily-v1"


@dataclass
class ReportContext:
    category: str
    category_name: str
    day: str
    papers: list[Paper]
    assets_dir: Path
    report_language: str = "zh"
    timezone: str = "Asia/Shanghai"
    generated_at: str = ""
    highlights: str = ""
    theme_notes: dict[str, str] = field(default_factory=dict)
    theme_labels: dict[str, tuple[str, str]] = field(default_factory=dict)  # code -> (zh, en)
    theme_order: list[str] = field(default_factory=list)
    llm_model: str = ""
    late_additions: int = 0
    font_regular: Path | None = None
    font_bold: Path | None = None
    notice: str = ""


# ---------------------------------------------------------------------------
# 资源与样式
# ---------------------------------------------------------------------------
def load_theme_taxonomy(path: Path) -> tuple[dict[str, tuple[str, str]], list[str]]:
    labels: dict[str, tuple[str, str]] = {}
    order: list[str] = []
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        for th in data.get("themes", []):
            code = th.get("code")
            if not code:
                continue
            labels[code] = (th.get("zh", code), th.get("en", code))
            order.append(code)
    except Exception as exc:  # noqa: BLE001
        log.warning("主题表读取失败 %s：%s", path, exc)
    labels.setdefault("other", ("其他", "Other"))
    if "other" not in order:
        order.append("other")
    return labels, order


def font_css(assets_dir: Path, regular: Path | None, bold: Path | None) -> str:
    """生成 @font-face；文件缺失时退回系统字体栈。

    思源黑体的拉丁扩展覆盖不全（例如 ř U+0159），因此随仓库再带一份
    DejaVu Sans 作为非中日韩字符的兜底，保证目标机器不装任何字体也不出豆腐块。
    """
    faces: list[str] = []
    fam = '"GRQC Sans"'
    latin = '"GRQC Latin"'
    if regular and Path(regular).exists():
        faces.append(
            f'@font-face{{font-family:{fam};font-weight:400;font-style:normal;'
            f'src:url("{Path(regular).resolve().as_uri()}") format("opentype");}}'
        )
    if bold and Path(bold).exists():
        faces.append(
            f'@font-face{{font-family:{fam};font-weight:700;font-style:normal;'
            f'src:url("{Path(bold).resolve().as_uri()}") format("opentype");}}'
        )
    fonts_dir = Path(assets_dir) / "fonts"
    for w, weight in (("DejaVuSans.ttf", 400), ("DejaVuSans-Bold.ttf", 700)):
        f = fonts_dir / w
        if f.exists():
            faces.append(
                f'@font-face{{font-family:{latin};font-weight:{weight};font-style:normal;'
                f'src:url("{f.resolve().as_uri()}") format("truetype");}}'
            )
    stack = (
        f'{fam}, {latin}, "Source Han Sans SC", "Noto Sans CJK SC", "Noto Sans SC", '
        '"PingFang SC", "Microsoft YaHei", "WenQuanYi Micro Hei", sans-serif'
    )
    return "\n".join(faces) + f"\nbody{{font-family:{stack};}}"


def resolve_fonts(cfg) -> tuple[Path | None, Path | None]:
    """按配置解析正文字体文件（返回 常规体, 粗体）。"""
    rep = cfg["report"]
    profile = rep.get("font_profile", "source-han")
    if profile == "system":
        return None, None
    if profile == "custom":
        r = rep.get("font_file_regular") or ""
        b = rep.get("font_file_bold") or ""
        return (cfg.resolve(r) if r else None), (cfg.resolve(b) if b else None)
    fonts = cfg.assets_dir / "fonts"
    reg = fonts / "SourceHanSansSC-Regular.otf"
    bold = fonts / "SourceHanSansSC-Bold.otf"
    return (reg if reg.exists() else None), (bold if bold.exists() else None)


def coverage_check(assets_dir: Path, texts: list[str]) -> list[str]:
    """自检：报告里出现的字符是否都被随仓库分发的字体覆盖。

    返回未被覆盖的字符列表（会打印告警，绝不静默出豆腐块）。
    """
    try:
        from fontTools.ttLib import TTFont
    except Exception:
        return []
    fonts_dir = Path(assets_dir) / "fonts"
    covered: set[int] = set()
    for name in (
        "SourceHanSansSC-Regular.otf",
        "SourceHanSansSC-Bold.otf",
        "DejaVuSans.ttf",
        "DejaVuSans-Bold.ttf",
    ):
        p = fonts_dir / name
        if not p.exists():
            continue
        try:
            f = TTFont(str(p), lazy=True)
            for t in f["cmap"].tables:
                covered |= set(t.cmap.keys())
            f.close()
        except Exception:
            continue
    if not covered:
        return []
    missing: list[str] = []
    seen: set[str] = set()
    for t in texts:
        for ch in t or "":
            if ch in seen or not ch.strip():
                continue
            seen.add(ch)
            if ord(ch) not in covered:
                missing.append(ch)
    return missing


def base_css(header_text: str, footer_text: str) -> str:
    return f"""
@page {{
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center {{ content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
                    font-size: 8.5pt; color: #666; }}
  @top-right {{ content: "{header_text}"; font-size: 8.5pt; color: #999; }}
}}
@page :first {{ @top-right {{ content: ""; }} }}
* {{ box-sizing: border-box; }}
body {{ font-size: 10.5pt; line-height: 1.7; color: #14181f; margin: 0; }}
h1 {{ font-size: 20pt; text-align: center; margin: 0 0 2mm; bookmark-level: 1; }}
.sub {{ text-align: center; color: #55606f; font-size: 9.5pt; margin-bottom: 7mm; }}
h2 {{ font-size: 13pt; border-bottom: 1px solid #b9c2cf; padding-bottom: 1mm;
      margin: 7mm 0 3mm; bookmark-level: 1; color: #1f2a3a; }}
h3 {{ font-size: 11pt; margin: 4mm 0 1mm; bookmark-level: 2; }}
.lead {{ background: #f6f8fb; border-left: 3px solid #6f9fdc; padding: 3mm 4mm; margin-bottom: 4mm; }}
.lead p {{ margin: 0 0 1.5mm; }}
.lead p:last-child {{ margin-bottom: 0; }}
.entry {{ margin-bottom: 4.5mm; break-inside: avoid; }}
.entry h3 {{ margin-top: 0; }}
.au {{ color: #55606f; font-size: 9.5pt; margin-bottom: 1mm; }}
.meta {{ color: #6b7787; font-size: 9pt; margin-bottom: 1mm; }}
.sum {{ font-size: 10.5pt; }}
.ref {{ font-size: 9.5pt; padding-left: 7mm; text-indent: -7mm; margin: 1.6mm 0; }}
.footer {{ margin-top: 8mm; border-top: 1px solid #dde3ea; padding-top: 2mm;
           color: #7a8698; font-size: 8.5pt; }}
.notice {{ color: #8a6d1f; background: #fdf7e3; border: 1px solid #f0e0b0;
           padding: 2mm 3mm; font-size: 9pt; margin-bottom: 4mm; }}
.math svg {{ vertical-align: middle; }}
.theme-head {{ font-size: 11.5pt; font-weight: 700; margin: 5mm 0 1mm; color: #22314a;
               bookmark-level: 2; }}
.theme-note {{ color: #45505f; font-size: 10pt; margin-bottom: 2mm; }}
.stats {{ text-align: center; color: #3c4a5e; font-size: 10pt; margin-bottom: 5mm; }}
"""


# ---------------------------------------------------------------------------
# 片段
# ---------------------------------------------------------------------------
def _p(text: str) -> str:
    """把模型输出的多行文本转成若干 <p>。"""
    lines = [ln.strip() for ln in (text or "").splitlines()]
    blocks = [ln for ln in lines if ln]
    if not blocks:
        return ""
    return "".join(f"<p>{html.escape(b)}</p>" for b in blocks)


def _theme_of(paper: Paper, order: list[str]) -> str:
    for t in paper.themes or []:
        if t in order:
            return t
    return "other"


def render_daily_html(ctx: ReportContext, math: MathService) -> str:
    lang = ctx.report_language if ctx.report_language in ("zh", "en") else "zh"
    papers = ctx.papers
    n_total = len(papers)
    n_primary = sum(1 for p in papers if p.primary_category == ctx.category)
    n_cross = n_total - n_primary
    header_text = f"{ctx.category} · {ctx.day}"
    generated = ctx.generated_at or dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    parts: list[str] = []
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>{html.escape(ctx.category)} {ctx.day}</title>")
    parts.append("<style>")
    parts.append(font_css(ctx.assets_dir, ctx.font_regular, ctx.font_bold))
    parts.append(base_css(header_text, ""))
    parts.append("</style></head><body>")

    # 标题
    parts.append(f"<h1>{html.escape(rt(lang, 'report.title', name=ctx.category_name))}</h1>")
    parts.append(
        f"<div class='sub'>{html.escape(rt(lang, 'report.subtitle', day=ctx.day, generated=generated))}</div>"
    )
    if ctx.notice:
        parts.append(f"<div class='notice'>{html.escape(ctx.notice)}</div>")
    if ctx.late_additions:
        parts.append(
            f"<div class='notice'>{html.escape(rt(lang, 'report.updated_note', n=ctx.late_additions, ts=generated))}</div>"
        )
    parts.append(
        "<div class='stats'>"
        + rt(lang, "report.stats", n=n_total, primary=n_primary, cross=n_cross)
        + "</div>"
    )

    # 今日要点
    if ctx.highlights:
        parts.append(f"<h2>{html.escape(rt(lang, 'report.highlights'))}</h2>")
        parts.append(f"<div class='lead'>{_p(ctx.highlights)}</div>")

    # 分类速览
    groups: dict[str, list[Paper]] = {}
    for p in papers:
        groups.setdefault(_theme_of(p, ctx.theme_order), []).append(p)
    ordered = [c for c in ctx.theme_order if c in groups] + [c for c in groups if c not in ctx.theme_order]

    if ctx.theme_notes or len(ordered) > 1:
        parts.append(f"<h2>{html.escape(rt(lang, 'report.themes'))}</h2>")
        for code in ordered:
            item = groups.get(code) or []
            label = (ctx.theme_labels.get(code) or (code, code))[0 if lang == "zh" else 1]
            parts.append(
                f"<div class='theme-head'>{html.escape(label)}"
                f"<span style='font-weight:400;color:#7a8698;font-size:9.5pt'>　"
                f"{html.escape(rt(lang, 'report.theme_count', n=len(item)))}</span></div>"
            )
            note = ctx.theme_notes.get(code)
            if note:
                parts.append(f"<div class='theme-note'>{_p(note)}</div>")

    # 论文清单
    parts.append(f"<h2>{html.escape(rt(lang, 'report.papers'))}</h2>")
    index: dict[str, int] = {}
    counter = 0
    for code in ordered:
        item = groups.get(code) or []
        if not item:
            continue
        label = (ctx.theme_labels.get(code) or (code, code))[0 if lang == "zh" else 1]
        parts.append(f"<h3>{html.escape(label)}</h3>")
        for p in item:
            counter += 1
            index[p.uid] = counter
            parts.append("<div class='entry'>")
            parts.append(f"<h3>[{counter}] {math.to_html(p.title)}</h3>")
            parts.append(f"<div class='au'>{html.escape(p.author_text())}</div>")
            cats = ", ".join(p.categories or [])
            parts.append(
                f"<div class='meta'>{html.escape(rt(lang, 'report.paper_meta', primary=p.primary_category, cats=cats))}"
                f" · <a href='{p.abs_url}'>arXiv:{p.arxiv_id}</a></div>"
            )
            if p.summary:
                parts.append(f"<div class='sum'>{math.to_html(p.summary)}</div>")
            elif not ctx.llm_model:
                parts.append(f"<div class='sum' style='color:#8a94a3'>{html.escape(rt(lang, 'report.no_llm'))}</div>")
            parts.append("</div>")

    # 参考文献
    parts.append(f"<h2>{html.escape(rt(lang, 'report.references'))}</h2>")
    for p in papers:
        i = index.get(p.uid, 0)
        parts.append(f"<div class='ref'>{html.escape(p.citation(i))}</div>")

    # 页脚
    foot = rt(lang, "report.footer")
    if ctx.llm_model:
        foot = rt(lang, "report.generated_by", model=ctx.llm_model) + " · " + foot
    parts.append(f"<div class='footer'>{html.escape(foot)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)
