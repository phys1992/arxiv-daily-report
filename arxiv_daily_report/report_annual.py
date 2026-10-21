"""年度报告：按子主题总结一整年的进展。

与日报/月报的区别：
  * 结构**以子主题为主线**——每个在日报或月报里出现过的主题各自成段；
  * 生成时**同时参考**当年的日报要点与月报正文（月报正文由 month_digest 表存档）；
  * 按自然年，1 月 1 日生成上一年；
  * 只覆盖本地确有日报的日期，不补历史缺口。

同样支持双模型：校验模型对照日报/月报挑错，生成模型据此修订一次。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .llm.client import LLMClient, LLMError
from .mathrender import MathService
from .pdf import render_pdf
from .render import base_css, font_css, font_preflight, font_stack
from .report_monthly import day_label, linkify_arxiv
from .render import load_theme_taxonomy
from .store import Store

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

PROMPT_VERSION = "annual-v1"

SYSTEM_ZH = """你是相对论与引力领域的资深综述作者，负责把一整年的工作按主题梳理成年度报告。

硬性规则：
1. 只能依据我提供的年度材料（月报正文与日报要点），不得引入任何外部知识或推测。
2. 不得编造论文、作者、结论或数值。
3. 术语用领域标准译名，表述与材料一致；逻辑完整、语法从简。
4. 每个主题写成连贯的年度回顾：先说这一年该方向的主线，再说具体进展及其关系，
   最后点出仍未解决的问题（只在材料提到时才写）。
5. 提到具体工作时用（2026年3月）或（2026年3月13日）标注它出现在哪期月报/哪天日报上；
   不要罗列篇数，不要逐条复述月报。
"""

SYSTEM_EN = """You are a senior reviewer in relativity and gravitation, writing an annual report
organised by theme.

Rules:
1. Use ONLY the supplied material (monthly briefs and daily digests); add no outside knowledge.
2. Never invent papers, authors, findings or numbers.
3. Keep terminology consistent with the material; be logically complete and concise.
4. Each theme should read as a coherent yearly review: the main line, the concrete advances and
   how they relate, and open problems only if the material mentions them.
5. Cite as (March 2026) or (13 March 2026); do not count papers or restate the monthly briefs.
"""


@dataclass
class AnnualData:
    year: str
    scope: str
    days: list[str] = field(default_factory=list)          # 有日报的日期
    months: list[str] = field(default_factory=list)        # 有月报的月份
    papers: list[str] = field(default_factory=list)        # 当年论文 arXiv 号
    theme_counts: dict[str, int] = field(default_factory=dict)
    theme_material: dict[str, list[str]] = field(default_factory=dict)  # code -> 材料行
    overview: str = ""
    themes: list[dict] = field(default_factory=list)       # [{code, text, refs, count}]
    model: str = ""
    reviewed: bool = False
    error: str = ""

    @property
    def paper_count(self) -> int:
        return len(self.papers)


# ---------------------------------------------------------------------------
# 汇总材料
# ---------------------------------------------------------------------------
def year_range(year: str) -> tuple[str, str, str, str]:
    """返回 (day_start, day_end, month_start, month_end) 半开区间字符串。"""
    y = int(str(year)[:4])
    return (f"{y}-01-01", f"{y + 1}-01-01", f"{y}-01", f"{y + 1}-01")


def collect_year(cfg: Config, store: Store, year: str) -> AnnualData:
    """把当年有日报的日期、月报正文与日报主题笔记汇总成年度材料。"""
    day_start, day_end, month_start, month_end = year_range(year)
    subs = [x for x in cfg.get("subscriptions", []) if x.get("enabled", True)]
    cats = [x.get("category") for x in subs if x.get("category")]
    data = AnnualData(year=str(year)[:4], scope="arXiv" if len(subs) > 1 else (cats[0] if cats else "gr-qc"))

    # ---- 日报 ----
    rows = store.month_digests(day_start, day_end, cats or None)
    for r in rows:
        data.days.append(r["day"])
        # month_digests 已经把 themes / theme_counts / papers 解析好了
        themes = r.get("themes") if isinstance(r.get("themes"), dict) else {}
        counts = r.get("theme_counts") if isinstance(r.get("theme_counts"), dict) else {}
        for code, n in counts.items():
            data.theme_counts[code] = data.theme_counts.get(code, 0) + int(n or 0)
        for code, note in themes.items():
            if not str(note).strip():
                continue
            data.theme_material.setdefault(code, []).append(
                f"[{r['day']} 日报] {str(note).strip()}"
            )
        papers = r.get("papers") if isinstance(r.get("papers"), list) else []
        for it in papers:
            pid = str((it or {}).get("id") or "").strip()
            if pid and pid not in data.papers:
                data.papers.append(pid)
    data.days = sorted(set(data.days))

    # ---- 月报 ----
    for brief in store.month_briefs(month_start, month_end, cats or None):
        month = brief["month"]
        if month not in data.months:
            data.months.append(month)
        head = f"[{month} 月报]"
        for t in brief.get("themes") or []:
            code = str((t or {}).get("code") or "")
            text = str((t or {}).get("text") or "").strip()
            if code and text:
                data.theme_material.setdefault(code, []).append(f"{head} {text}")
        if brief.get("overview"):
            data.theme_material.setdefault("__overview__", []).append(
                f"{head} {brief['overview']}"
            )
    # 引用校验用「有日报或月报的月份」：某月即使没生成月报，
    # 只要当月有日报，(2026年10月) 也是合法的时间引用。
    data.months = sorted(set(data.months) | {d[:7] for d in data.days})
    for code in list(data.theme_material):
        if code == "__overview__":
            continue
        data.theme_material[code] = sorted(data.theme_material[code])
    return data


# ---------------------------------------------------------------------------
# 模型：逐主题生成 → 总览 → 校验 → 修订
# ---------------------------------------------------------------------------
_THEME_SCHEMA = '{"code":"主题代码","text":"该主题的年度回顾（150–250 字）","refs":["2026-03","2026-03-13"]}'
_OVERVIEW_SCHEMA = '{"overview":"年度总览，250–400 字，连贯成段"}'


def _valid_refs(raw, days: list[str], months: list[str]) -> list[str]:
    """只保留真实存在的月份/日期引用。"""
    out: list[str] = []
    for item in raw or []:
        s = str(item).strip()[:10]
        if re.fullmatch(r"\d{4}-\d{2}", s) and s in months:
            out.append(s)
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", s) and s in days:
            out.append(s)
    return sorted(set(out))


def annual_llm(
    client: LLMClient, data: AnnualData, *, lang: str, labels: dict, order: list[str],
    progress: Progress, cfg: Config | None = None, store: Store | None = None,
) -> None:
    """逐主题生成年度回顾，再合成总览；最后交给校验模型核对并修订一次。"""
    zh = lang != "en"
    codes = [c for c in data.theme_material if c != "__overview__" and data.theme_material[c]]
    codes.sort(key=lambda c: (-data.theme_counts.get(c, 0),
                              order.index(c) if c in order else 999))
    if not codes:
        data.error = "当年没有可用的主题材料"
        return

    system = SYSTEM_ZH if zh else SYSTEM_EN
    overview_lines = data.theme_material.get("__overview__", [])
    base_ctx = (
        f"年份：{data.year}\n覆盖范围：{data.scope}\n"
        f"有日报的日期共 {len(data.days)} 天（{data.days[0]} ~ {data.days[-1]}）\n"
        f"有月报的月份：{', '.join(data.months) or '（无）'}\n"
    )
    if overview_lines:
        base_ctx += "\n各月月报的总览（供把握全年主线）：\n" + "\n".join(overview_lines) + "\n"

    # ---- map：逐主题 ----
    results: list[dict] = []
    for code in codes:
        label = labels.get(code, (code, code))[0 if zh else 1]
        material = "\n".join(data.theme_material[code])
        if len(material) > 16000:
            material = material[:16000] + "\n…（材料过长，已截断）"
        progress(f"  年报主题：{label}（{data.theme_counts.get(code, 0)} 篇）")
        ask = (
            f"主题：{label}（代码 {code}）\n"
            f"该主题全年论文数：{data.theme_counts.get(code, 0)}\n\n"
            f"材料（按时间排列的月报正文与日报要点）：\n{material}\n\n"
            + ("请写这个主题的年度回顾，150–250 字。只依据上面的材料；"
               "提到具体工作时用（2026年3月）或（2026年3月13日）标注出处。"
               if zh else
               "Write a 120-200 word yearly review of this theme, citing (March 2026) or "
               "(13 March 2026).")
            + f"\n请输出 JSON：{_THEME_SCHEMA}"
        )
        try:
            obj = client.chat_json(system, base_ctx + "\n" + ask,
                                   schema_hint=_THEME_SCHEMA, max_tokens=1200)
        except LLMError as exc:
            progress(f"    该主题生成失败：{exc}")
            continue
        text = " ".join(str(obj.get("text") or "").split())
        if not text:
            continue
        results.append({
            "code": code,
            "text": text,
            "refs": _valid_refs(obj.get("refs"), data.days, data.months),
            "count": data.theme_counts.get(code, 0),
        })

    if not results:
        data.error = "所有主题都没能生成"
        return
    data.themes = results

    # ---- reduce：总览 ----
    progress("  年报总览：综合各主题")
    brief = "\n".join(
        f"- {labels.get(t['code'], (t['code'], t['code']))[0 if zh else 1]}：{t['text']}"
        for t in results
    )
    ask2 = (
        (f"{base_ctx}\n各主题的年度回顾：\n{brief}\n\n"
         "请写一段年度总览，250–400 字，连贯成段：先说这一年整体在做什么，"
         "再点出最突出的方向与它们之间的关系。不要逐条复述主题内容。"
         if zh else
         f"{base_ctx}\nTheme reviews:\n{brief}\n\nWrite a 200-320 word coherent overview.")
        + f"\n请输出 JSON：{_OVERVIEW_SCHEMA}"
    )
    try:
        obj = client.chat_json(system, ask2, schema_hint=_OVERVIEW_SCHEMA, max_tokens=1600)
        data.overview = " ".join(str(obj.get("overview") or "").split())
    except LLMError as exc:
        progress(f"  总览生成失败（保留主题部分）：{exc}")

    # ---- 校验与修订 ----
    if cfg is not None:
        try:
            _review_annual(client, cfg, store, data, base_ctx=base_ctx,
                           labels=labels, lang=lang, progress=progress)
        except Exception as exc:  # noqa: BLE001 - 校验失败不该拖垮年报
            progress(f"  校验与修订失败（沿用初版）：{exc}")


def _review_annual(client: LLMClient, cfg: Config, store: Store | None, data: AnnualData, *,
                   base_ctx: str, labels: dict, lang: str, progress: Progress) -> None:
    from .review import review_and_revise, verify_client

    verifier = verify_client(cfg, store)
    if verifier is None or not data.themes:
        return
    progress(f"  校验模型 {verifier.describe()} 对照日报与月报核对…")
    draft = {"overview": data.overview, "themes": data.themes}
    material = "\n\n".join(
        f"## {labels.get(c, (c, c))[0 if lang != 'en' else 1]}（{c}）\n" + "\n".join(lines)
        for c, lines in data.theme_material.items() if c != "__overview__"
    )
    source = f"{base_ctx}\n\n全年材料（按主题）：\n{material[:24000]}"
    checklist = (
        "这是一份年报，材料是当年的月报正文与日报要点。请核对：\n"
        "1. 每个主题回顾里提到的进展，是否真的出现在该主题的材料里；\n"
        "2. 引用的月份/日期是否真实存在于材料中；\n"
        "3. 是否把不同月份的工作混为一谈，或把推测写成了结论；\n"
        "4. 总览是否引入了主题回顾之外的内容；\n"
        "5. 是否遗漏了材料里明确提到的重要工作。"
    )
    hint = ('{"overview":"...","themes":[{"code":"...","text":"...",'
            '"refs":["2026-03"],"count":0}]}')
    revised, _verdict = review_and_revise(
        client, verifier, draft=draft, source=source, subject="年度报告",
        checklist=checklist,
        system=SYSTEM_ZH if lang != "en" else SYSTEM_EN,
        output_hint=hint, max_tokens=8000, progress=progress,
    )
    if revised is None:
        return
    if str(revised.get("overview") or "").strip():
        data.overview = " ".join(str(revised["overview"]).split())
    by_code = {t["code"]: t for t in data.themes}
    for it in revised.get("themes") or []:
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "")
        text = " ".join(str(it.get("text") or "").split())
        if code in by_code and text:
            by_code[code]["text"] = text
            refs = _valid_refs(it.get("refs"), data.days, data.months)
            if refs:
                by_code[code]["refs"] = refs
    data.reviewed = True
    progress("  已按校验意见修订年报")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _ref_text(refs: list[str], lang: str) -> str:
    if not refs:
        return ""
    zh = lang != "en"
    parts = []
    for r in refs:
        if len(r) == 7:
            y, m = r.split("-")
            parts.append(f"{y}年{int(m)}月" if zh else f"{dt.date(int(y), int(m), 1):%B %Y}")
        else:
            parts.append(day_label(r, "zh" if zh else "en"))
    return "、".join(parts) if zh else ", ".join(parts)


def render_annual_html(cfg: Config, data: AnnualData, *, scope_name: str,
                       math: MathService | None = None) -> str:
    lang = cfg.get("report_language", "zh")
    bilingual = lang == "zh-en"
    if lang not in ("zh", "en"):
        lang = "zh"
    labels, _order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    generated = dt.datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")

    def h2(zh: str, en: str) -> str:
        return "<h2>" + (f"{zh} / {en}" if bilingual else (zh if lang == "zh" else en)) + "</h2>"

    title = (f"{scope_name} {data.year} 年度报告" if lang == "zh"
             else f"{scope_name} {data.year} Annual Report")
    if bilingual:
        title = f"{scope_name} {data.year} 年度报告 / Annual Report"
    if lang == "zh":
        span = (f"{day_label(data.days[0])}–{day_label(data.days[-1])}"
                if len(data.days) > 1 else (day_label(data.days[0]) if data.days else ""))
        subtitle = (f"{data.year} 年 · 覆盖 {span} · {len(data.days)} 天日报、"
                    f"{len(data.months)} 期月报 · 论文 {data.paper_count} 篇 · 生成于 {generated}")
    else:
        span = (f"{day_label(data.days[0], 'en')}–{day_label(data.days[-1], 'en')}"
                if len(data.days) > 1 else (day_label(data.days[0], "en") if data.days else ""))
        subtitle = (f"{data.year} · covering {span} · {len(data.days)} daily digests, "
                    f"{len(data.months)} monthly briefs · {data.paper_count} papers · {generated}")

    parts = ["<!doctype html><html><head><meta charset='utf-8'>"]
    parts.append(f"<title>{html.escape(title)}</title><style>")
    parts.append(font_css(cfg))
    parts.append(base_css(f"{scope_name} · {data.year}", "", font_stack(cfg)))
    parts.append("""
.abstract { background: #f6f9fc; border-left: 3px solid #2d6ca2; padding: 4mm 5mm; margin: 0 0 6mm; }
.abstract p { margin: 0 0 2.5mm; }
.theme { margin: 0 0 7mm; break-inside: avoid; }
.theme .name { font-weight: 700; font-size: 12.5pt; }
.theme .meta { color: #6b7785; font-size: 9pt; margin-left: 2mm; }
.theme .refs { color: #6b7785; font-size: 9pt; margin-top: 1.5mm; }
</style></head><body>""")
    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(f"<div class='sub'>{html.escape(subtitle)}</div>")

    if not data.days:
        parts.append("<p>" + ("这一年没有可用的日报摘要。" if lang == "zh"
                              else "No daily digests found for this year.") + "</p>")

    render = math.to_html if math is not None else (lambda s: linkify_arxiv(s))

    if data.overview:
        parts.append(h2("全年概览", "Overview"))
        parts.append("<div class='abstract'>")
        for para in data.overview.splitlines():
            if para.strip():
                parts.append(f"<p>{render(para.strip())}</p>")
        parts.append("</div>")

    if data.themes:
        parts.append(h2("分主题年度回顾", "By theme"))
        for t in data.themes:
            label = labels.get(t["code"], (t["code"], t["code"]))[0 if lang == "zh" else 1]
            meta = ""
            if t.get("count"):
                meta = (f" <span class='meta'>{t['count']} 篇</span>" if lang == "zh"
                        else f" <span class='meta'>{t['count']} papers</span>")
            refs = _ref_text(t.get("refs") or [], lang)
            refs_html = (f"<div class='refs'>" + ("出处：" if lang == "zh" else "Cited: ")
                         + html.escape(refs) + "</div>") if refs else ""
            parts.append(
                f"<div class='theme'><span class='name'>{html.escape(label)}</span>{meta}"
                f"<p>{render(t['text'])}</p>{refs_html}</div>"
            )

    foot = ("内容取自当年的日报要点与月报正文，不另列文献；括号与出处标明对应的时间。"
            if lang == "zh" else
            "Condensed from the year's daily digests and monthly briefs; dates give the source.")
    if data.model:
        foot += ("　摘编模型：" if lang == "zh" else "  Model: ") + data.model
    parts.append(f"<div class='footer'>{html.escape(foot)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def run_annual(
    cfg: Config, store: Store, *, year: str | None = None,
    use_llm: bool = True, progress: Progress | None = None,
) -> list[Path]:
    say: Progress = progress or (lambda m: log.info("%s", m))
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    if not year:
        year = str(dt.datetime.now(ZoneInfo(tz)).year - 1)
    year = str(year)[:4]

    say(f"生成年报：{year}")
    data = collect_year(cfg, store, year)
    if not data.days:
        say(f"  {year} 年没有日报，跳过")
        return []
    say(f"  覆盖 {data.days[0]} ~ {data.days[-1]}：{len(data.days)} 天日报、"
        f"{len(data.months)} 期月报、{data.paper_count} 篇论文、{len(data.theme_counts)} 个主题")

    labels, order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))
    if use_llm:
        client = LLMClient(cfg, store)
        if client.ready:
            say(f"  调用模型 {client.describe()} 撰写年度报告")
            annual_llm(client, data, lang=cfg.get("report_language", "zh"), labels=labels,
                       order=order, progress=say, cfg=cfg, store=store)
            data.model = client.describe()
        else:
            say("  未配置模型密钥，只出统计版")
    else:
        say("  已跳过模型（--no-llm）")

    for w in font_preflight(cfg):
        say("  字体提醒：" + w)

    math = MathService(store, assets_dir=cfg.assets_dir,
                       backend=cfg["report"].get("math_backend", "auto"))
    texts = [data.overview] + [t["text"] for t in data.themes]
    math.prepare(texts)

    scope_slug = re.sub(r"[^A-Za-z0-9._-]", "_", data.scope) or "arXiv"
    out_dir = cfg.reports_dir / "annual" / scope_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{year}.pdf"
    html_text = render_annual_html(cfg, data, scope_name=data.scope, math=math)
    _, backend = render_pdf(html_text, pdf, base_url=cfg.assets_dir)
    say(f"  年报生成（{backend}）：{pdf}")
    store.record_report(year, data.scope, str(pdf), data.paper_count, kind="annual")
    store.save_annual_digest(
        year, data.scope, overview=data.overview,
        themes=[{"code": t["code"], "text": t["text"], "refs": t["refs"]} for t in data.themes],
        model=data.model, reviewed=data.reviewed,
    )
    return [pdf]


def should_run_annual(cfg: Config, now: dt.datetime | None = None) -> str | None:
    """北京时间每年 1 月 1 日生成上一年年报。"""
    sched = cfg["schedule"]
    if not sched.get("auto_annual", True):
        return None
    tz = ZoneInfo(sched.get("timezone", "Asia/Shanghai"))
    now = now or dt.datetime.now(tz)
    now = now.astimezone(tz)
    if (now.month, now.day) != (1, 1):
        return None
    return str(now.year - 1)
