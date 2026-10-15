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
    notice: str = ""
    bilingual: bool = False
    font_css_text: str = ""      # 由调用方用 font_css(cfg) 生成；空则用默认栈
    font_stack_text: str = ""    # 由调用方用 font_stack(cfg) 生成


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


# 字体由系统提供，不再随仓库分发。
# 顺序规则：西文在前，中文在后。
#   西文字体列表负责英文/数字/符号；汉字在西文字体里没有字形，会自动回退到中文字体列表。
#   若把中文字体放前面，英文也会用中文字体的拉丁字形 —— 所以顺序不能反。
DEFAULT_LATIN_FONTS = (
    '"DejaVu Sans", "Liberation Sans", "Noto Sans", "Nimbus Sans", '
    '"Helvetica Neue", Helvetica, Arial'
)
DEFAULT_CJK_FONTS = (
    '"Source Han Sans SC", "Noto Sans CJK SC", "Noto Sans SC", "\u601d\u6e90\u9ed1\u4f53", '
    '"WenQuanYi Micro Hei", "WenQuanYi Zen Hei", "Microsoft YaHei", "PingFang SC", '
    '"SimHei", "AR PL UMing CN"'
)


def font_stack(cfg=None) -> str:
    """正文字体栈（西文优先 + 中文回退 + 系统兜底）。"""
    if cfg is not None:
        custom = (cfg["report"].get("font_family") or "").strip()
        if custom:
            return custom
    return f"{DEFAULT_LATIN_FONTS}, {DEFAULT_CJK_FONTS}, sans-serif"


def font_css(cfg) -> str:
    """生成字体相关 CSS。

    默认不产生任何 @font-face：中英文字体都交给系统提供（见 README 的字体要求）。
    只有在用户显式配置了 font_file 时才把它内联进来并置于栈首。
    """
    faces: list[str] = []
    custom = resolve_font_file(cfg)
    family = '"GRQC Custom"'
    if custom is not None:
        fmt = "truetype" if custom.suffix.lower() in (".ttf", ".ttc") else "opentype"
        faces.append(
            f'@font-face{{font-family:{family};src:url("{custom.as_uri()}") format("{fmt}");}}'
        )
    stack = f"{family}, {font_stack(cfg)}" if faces else font_stack(cfg)
    # html 与 body 都要声明：@page 的页眉页脚不继承 body 的字体
    return "\n".join(faces) + f"\nhtml{{font-family:{stack};}}\nbody{{font-family:{stack};}}"


def resolve_font_file(cfg) -> Path | None:
    """用户可选的自定义字体文件。默认没有，即完全使用系统字体。"""
    f = (cfg["report"].get("font_file") or "").strip()
    if not f:
        return None
    p = cfg.resolve(f)
    if not p.exists():
        log.warning("配置的字体文件不存在：%s", p)
        return None
    return p


def font_preflight() -> list[str]:
    """检查系统字体是否够用，返回告警列表（不阻断出报告）。

    以前是逐字符比对随仓库分发的字体 cmap；现在字体由系统提供，
    改为向 fontconfig 询问：系统到底有没有中文字体。
    """
    import shutil as _shutil
    import subprocess as _sp

    warnings: list[str] = []
    if not _shutil.which("fc-list"):
        return warnings                       # 没有 fontconfig 就跳过检查
    try:
        out = _sp.run(["fc-list", ":lang=zh-cn", "family"],
                      capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:  # noqa: BLE001
        return warnings
    if not out:
        warnings.append(
            "系统没有检测到任何中文字体，报告里的汉字会显示为方块。"
            "请安装中文字体，例如 Debian/Ubuntu：sudo apt install fonts-noto-cjk；"
            "Fedora：sudo dnf install google-noto-sans-cjk-fonts。"
        )
        return warnings
    flat = out.lower()
    if "source han" not in flat and "\u601d\u6e90" not in flat:
        warnings.append(
            "未检测到思源黑体，将使用系统默认中文字体（可正常显示，字形风格可能不同）。"
            "安装思源黑体可得到稳定一致的排版：sudo apt install fonts-noto-cjk"
        )
    return warnings



def base_css(header_text: str, footer_text: str = "", stack: str | None = None) -> str:
    """页面样式。页眉页脚必须显式声明字体栈（它们不继承 body）。"""
    fam = stack or font_stack()
    return f"""
@page {{
  size: A4;
  margin: 18mm 16mm 20mm 16mm;
  @bottom-center {{ content: "第 " counter(page) " 页 / 共 " counter(pages) " 页";
                    font-family: {fam}; font-size: 8.5pt; color: #666; }}
  @top-right {{ content: "{header_text}"; font-family: {fam};
                font-size: 8.5pt; color: #999; }}
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
.en-label {{ font-weight: 400; font-size: 0.78em; color: #7a8698; }}
"""


# ---------------------------------------------------------------------------
# 片段
# ---------------------------------------------------------------------------
def heading(lang: str, key: str, bilingual: bool = False) -> str:
    """章节标题；中英对照模式下追加英文小标题。"""
    if bilingual:
        return (f"{rt('zh', key)}<span class='en-label'>&nbsp;/&nbsp;{rt('en', key)}</span>")
    return rt(lang, key)


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
    stack = ctx.font_stack_text or font_stack()
    parts.append("<!doctype html><html><head><meta charset='utf-8'>")
    parts.append(f"<title>{html.escape(ctx.category)} {ctx.day}</title>")
    parts.append("<style>")
    parts.append(ctx.font_css_text or f"body{{font-family:{stack};}}")
    parts.append(base_css(header_text, "", stack))
    parts.append("</style></head><body>")

    # 标题
    if ctx.bilingual:
        t = (f"{ctx.category_name} 论文日报"
             f"<span class='en-label'>&nbsp;/&nbsp;Daily Digest</span>")
        parts.append(f"<h1>{t}</h1>")
    else:
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
        parts.append(f"<h2>{heading(lang, 'report.highlights', ctx.bilingual)}</h2>")
        parts.append(f"<div class='lead'>{_p(ctx.highlights)}</div>")

    # 分类速览
    groups: dict[str, list[Paper]] = {}
    for p in papers:
        groups.setdefault(_theme_of(p, ctx.theme_order), []).append(p)
    ordered = [c for c in ctx.theme_order if c in groups] + [c for c in groups if c not in ctx.theme_order]

    if ctx.theme_notes or len(ordered) > 1:
        parts.append(f"<h2>{heading(lang, 'report.themes', ctx.bilingual)}</h2>")
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
    parts.append(f"<h2>{heading(lang, 'report.papers', ctx.bilingual)}</h2>")
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

    # 不再单列参考文献：论文清单里已含作者、标题、arXiv 号与可点击链接

    # 页脚
    foot = rt(lang, "report.footer")
    if ctx.llm_model:
        foot = rt(lang, "report.generated_by", model=ctx.llm_model) + " · " + foot
    parts.append(f"<div class='footer'>{html.escape(foot)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)
