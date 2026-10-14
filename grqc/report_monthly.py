"""月报：把当月日报的结构化结果做 map-reduce，产出更精简的分类总结。

分层策略（质量与成本兼顾）：
  map    —— 按主题分组，逐主题喂入「标题 + 当日要点」，生成该主题的月度主线
  reduce —— 汇总各主题主线，生成月度总览与趋势判断
统计数字（篇数、主题分布、环比）全部由代码计算，不交给模型。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .i18n import rt
from .llm.client import LLMClient, LLMError
from .mathrender import MathService
from .models import Paper
from .pdf import render_pdf
from .render import base_css, coverage_check, font_css, load_theme_taxonomy, resolve_fonts
from .store import Store

log = logging.getLogger(__name__)
Progress = Callable[[str], None]

PROMPT_VERSION = "monthly-v1"

SYSTEM_ZH = """你是相对论与引力领域的科研助理，正在撰写月度研究综述。

硬性规则：
1. 只能依据我提供的论文标题与当日要点，不得引入外部知识，不得编造。
2. 引用论文必须使用我给出的编号，例如 [12]，不得引用未提供的编号。
3. 输出简体中文，专有名词保留英文。
4. 只输出 JSON，不要输出解释文字或 Markdown 代码块。"""

SYSTEM_EN = """You are a research assistant in relativity and gravitation writing a monthly review.
Rules: use ONLY the supplied titles and notes; never invent; cite with my numeric labels like [12];
output JSON only, no extra text or fences."""


@dataclass
class MonthlyData:
    month: str
    papers: list[Paper] = field(default_factory=list)
    by_theme: dict[str, list[Paper]] = field(default_factory=dict)
    by_category: Counter = field(default_factory=Counter)
    theme_stats: dict[str, int] = field(default_factory=dict)
    overview: str = ""
    theme_notes: dict[str, str] = field(default_factory=dict)
    model: str = ""
    prev_month_count: int = 0


def _join(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    return f"{a}\n{b}" if a and b else (a or b)


def month_range(month: str, tz: str = "Asia/Shanghai") -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def collect_month(cfg: Config, store: Store, month: str) -> MonthlyData:
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    start, end = month_range(month, tz)
    rows = store.conn.execute(
        f"SELECT payload FROM papers WHERE day_bj>=? AND day_bj<? AND version=1 ORDER BY day_bj",
        (start, end),
    ).fetchall()
    papers = [Paper.from_dict(json.loads(r["payload"])) for r in rows]
    data = MonthlyData(month=month, papers=papers)

    labels, order = load_theme_taxonomy(cfg.data_dir / "theme_taxonomy.json")
    groups: dict[str, list[Paper]] = defaultdict(list)
    for p in papers:
        code = next((t for t in (p.themes or []) if t in labels), "other")
        groups[code].append(p)
    data.by_theme = dict(groups)
    data.theme_stats = {c: len(v) for c, v in groups.items()}
    data.by_category = Counter(p.primary_category for p in papers)

    pstart, pend = month_range(prev_month(month), tz)
    row = store.conn.execute(
        "SELECT COUNT(*) c FROM papers WHERE day_bj>=? AND day_bj<? AND version=1",
        (pstart, pend),
    ).fetchone()
    data.prev_month_count = int(row["c"])
    return data


# ---------------------------------------------------------------------------
def _paper_lines(papers: list[Paper], start_index: int = 1) -> tuple[list[str], dict[str, int]]:
    lines, index = [], {}
    for i, p in enumerate(papers, start_index):
        index[p.uid] = i
        note = p.summary or p.abstract[:400]
        lines.append(f"[{i}] {p.title} —— {note}")
    return lines, index


def _chunk(lines: list[str], max_chars: int = 30000, max_items: int = 60) -> list[list[str]]:
    """按字符数与条数切块，避免单次提示过长。"""
    out: list[list[str]] = []
    cur: list[str] = []
    size = 0
    for ln in lines:
        if cur and (size + len(ln) > max_chars or len(cur) >= max_items):
            out.append(cur)
            cur, size = [], 0
        cur.append(ln)
        size += len(ln) + 1
    if cur:
        out.append(cur)
    return out


def _theme_note(
    client: LLMClient, theme_label: str, lines: list[str], *, lang: str, month: str,
    progress: Progress,
) -> str:
    """单个主题的月度主线：必要时分块生成后再合并。"""
    blocks = _chunk(lines)
    if not blocks:
        return ""
    ask = (
        '请输出 JSON：{"note":"该主题本月的 150–250 字研究主线综述，引用格式 [12]，'
        '说明主要进展方向、方法与结论上的共性"}'
        if lang == "zh"
        else 'Return JSON: {"note":"150-250 word overview of this theme this month, cite as [12]"}'
    )
    notes: list[str] = []
    for i, blk in enumerate(blocks, 1):
        tag = f"（第 {i}/{len(blocks)} 批）" if len(blocks) > 1 else ""
        progress(f"  月报 map：{theme_label}{tag}")
        user = (
            f"月份：{month}\n主题：{theme_label}\n\n论文（编号 标题 —— 要点）：\n"
            + "\n".join(blk) + "\n\n" + ask
        )
        try:
            obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                                   schema_hint='{"note":"..."}', max_tokens=1500)
            note = str(obj.get("note") or "").strip()
            if note:
                notes.append(note)
        except LLMError as exc:
            progress(f"    {theme_label} 第 {i} 批失败：{exc}")
    if not notes:
        return ""
    if len(notes) == 1:
        return notes[0]
    progress(f"  月报 merge：合并 {theme_label} 的 {len(notes)} 批")
    user = (
        f"月份：{month}\n主题：{theme_label}\n\n以下是同一主题分批生成的综述片段，"
        f"请合并为一段连贯的 200–300 字综述，去除重复，保留引用编号：\n\n"
        + "\n\n".join(f"片段{i}：{n}" for i, n in enumerate(notes, 1))
        + '\n\n请输出 JSON：{"note":"合并后的综述"}'
    )
    try:
        obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                               schema_hint='{"note":"..."}', max_tokens=1500)
        return str(obj.get("note") or "").strip() or " ".join(notes)
    except LLMError:
        return " ".join(notes)


def monthly_llm(
    client: LLMClient, data: MonthlyData, *, lang: str, labels: dict, order: list[str],
    progress: Progress,
) -> None:
    """map：逐主题主线（必要时分块）；reduce：月度总览。"""
    theme_lines: dict[str, list[str]] = {}
    counter = 1
    for code in order:
        papers = data.by_theme.get(code) or []
        if not papers:
            continue
        lines, _ = _paper_lines(papers, counter)
        counter += len(papers)
        theme_lines[code] = lines

    if not theme_lines:
        return

    for code, lines in theme_lines.items():
        label = labels.get(code, (code, code))[0 if lang == "zh" else 1]
        note = _theme_note(client, label, lines, lang=lang, month=data.month, progress=progress)
        if note:
            data.theme_notes[code] = note

    # reduce：月度总览
    progress("  月报 reduce：生成月度总览")
    theme_summary = "\n".join(
        f"- {labels.get(c, (c, c))[0 if lang == 'zh' else 1]}：{n}"
        for c, n in sorted(data.theme_stats.items(), key=lambda kv: -kv[1])
    )
    stats_line = (
        f"本月共 {len(data.papers)} 篇新论文；上月 {data.prev_month_count} 篇；"
        f"主分类分布：{dict(data.by_category.most_common(8))}"
    )
    detail = "\n\n".join(
        f"### {labels.get(c, (c, c))[0 if lang == 'zh' else 1]}\n{data.theme_notes.get(c, '')}"
        for c in order if data.theme_notes.get(c)
    )
    user = (
        f"月份：{data.month}\n统计：{stats_line}\n主题分布：\n{theme_summary}\n\n各主题综述：\n{detail}\n\n"
        + (
            '请输出 JSON：{"overview":"400–700 字的月度总览。要求：指出本月最突出的 3–5 个方向、'
            '方向之间的关联、以及值得关注的方法或结论；引用论文用 [编号]；不要罗列篇数，要讲清脉络"}'
            if lang == "zh"
            else 'Return JSON: {"overview":"400-700 word monthly overview with 3-5 highlights, cite as [n]"}'
        )
    )
    try:
        obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                               schema_hint='{"overview":"..."}', max_tokens=3000)
        data.overview = str(obj.get("overview") or "").strip()
    except LLMError as exc:
        progress(f"  月度总览失败：{exc}")


# ---------------------------------------------------------------------------
def render_monthly_html(
    cfg: Config, data: MonthlyData, math: MathService, *,
    scope_name: str, kind: str = "monthly",
) -> str:
    lang = cfg.get("report_language", "zh")
    if lang == "zh-en":
        lang = "zh"
    labels, order = load_theme_taxonomy(cfg.data_dir / "theme_taxonomy.json")
    reg, bold = resolve_fonts(cfg)
    generated = dt.datetime.now(ZoneInfo(cfg["schedule"].get("timezone", "Asia/Shanghai"))).strftime("%Y-%m-%d %H:%M %Z")

    y, m = data.month.split("-")
    title = (
        f"{scope_name} 月度综述"
        if lang == "zh"
        else f"{scope_name} Monthly Review"
    )
    subtitle = (
        f"{y} 年 {int(m)} 月 · 共 {len(data.papers)} 篇新论文 · 生成于 {generated}"
        if lang == "zh"
        else f"{y}-{m} · {len(data.papers)} new papers · generated {generated}"
    )

    parts: list[str] = ["<!doctype html><html><head><meta charset='utf-8'>"]
    parts.append(f"<title>{html.escape(title)}</title><style>")
    parts.append(font_css(cfg.assets_dir, reg, bold))
    parts.append(base_css(f"{scope_name} · {data.month}", ""))
    parts.append(
        "h4{font-size:11pt;margin:4mm 0 1mm;bookmark-level:2}"
        ".compact{font-size:9.5pt;padding-left:6mm;text-indent:-6mm;margin:1.2mm 0}"
        ".stat-table{width:100%;border-collapse:collapse;font-size:10pt;margin:2mm 0}"
        ".stat-table th,.stat-table td{border-bottom:1px solid #e6ebf1;padding:1.6mm 2mm;text-align:left}"
        ".stat-table th{color:#55606f;font-weight:700}"
        "</style></head><body>")
    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(f"<div class='sub'>{html.escape(subtitle)}</div>")

    # 统计
    delta = len(data.papers) - data.prev_month_count
    pct = (delta / data.prev_month_count * 100) if data.prev_month_count else 0
    bilingual = cfg.get("report_language", "zh") == "zh-en"

    def h2(zh: str, en: str) -> str:
        label = f"{zh} / {en}" if bilingual else (zh if lang == "zh" else en)
        return "<h2>" + label + "</h2>"

    parts.append(h2("当月统计", "Statistics"))
    parts.append("<table class='stat-table'><tr>"
                 + ("<th>指标</th><th>数值</th>" if lang == "zh" else "<th>Metric</th><th>Value</th>")
                 + "</tr>")
    rows = [
        ("新论文总数" if lang == "zh" else "New papers", str(len(data.papers))),
        ("上月同期" if lang == "zh" else "Previous month", str(data.prev_month_count)),
        ("环比" if lang == "zh" else "Change",
         f"{'+' if delta >= 0 else ''}{delta} ({pct:+.1f}%)"),
        ("涉及主分类" if lang == "zh" else "Primary categories", str(len(data.by_category))),
        ("主题数" if lang == "zh" else "Themes", str(len(data.theme_stats))),
    ]
    for k, v in rows:
        parts.append(f"<tr><td>{html.escape(k)}</td><td>{html.escape(v)}</td></tr>")
    parts.append("</table>")

    parts.append("<h4>" + ("主题分布" if lang == "zh" else "Theme distribution") + "</h4>")
    for code, n in sorted(data.theme_stats.items(), key=lambda kv: -kv[1]):
        label = labels.get(code, (code, code))[0 if lang == "zh" else 1]
        parts.append(f"<div class='compact'>{html.escape(label)}：{n} 篇</div>")

    # 总览
    if data.overview:
        parts.append(h2("月度总览", "Monthly overview"))
        for line in data.overview.splitlines():
            if line.strip():
                parts.append(f"<p>{html.escape(line.strip())}</p>")

    # 分主题总结
    if data.theme_notes:
        parts.append(h2("分类总结", "Thematic summaries"))
        for code in order:
            if code not in data.theme_notes:
                continue
            label = labels.get(code, (code, code))[0 if lang == "zh" else 1]
            parts.append(f"<h3>{html.escape(label)}（{data.theme_stats.get(code, 0)} 篇）</h3>")
            for line in data.theme_notes[code].splitlines():
                if line.strip():
                    parts.append(f"<p>{html.escape(line.strip())}</p>")

    # 论文清单 + 参考文献
    parts.append(h2("当月论文清单", "Papers this month"))
    index: dict[str, int] = {}
    counter = 0
    for code in order:
        papers = data.by_theme.get(code) or []
        if not papers:
            continue
        label = labels.get(code, (code, code))[0 if lang == "zh" else 1]
        parts.append(f"<h3>{html.escape(label)}（{len(papers)} 篇）</h3>")
        for p in papers:
            counter += 1
            index[p.uid] = counter
            parts.append(
                f"<div class='compact'>[{counter}] {math.to_html(p.title)}"
                f"　<span style='color:#6b7787'>arXiv:{p.arxiv_id} · {html.escape(p.primary_category)}"
                f" · {p.day_str('Asia/Shanghai')}</span></div>"
            )
    parts.append("<h2>" + rt(lang, "report.references") + (" / References" if bilingual else "") + "</h2>")
    for code in order:
        for p in data.by_theme.get(code) or []:
            i = index.get(p.uid, 0)
            parts.append(f"<div class='ref'>{html.escape(p.citation(i))}</div>")

    foot = rt(lang, "report.footer")
    if data.model:
        foot = rt(lang, "report.generated_by", model=data.model) + " · " + foot
    parts.append(f"<div class='footer'>{html.escape(foot)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


def run_monthly(
    cfg: Config, store: Store, *, month: str | None = None,
    use_llm: bool = True, progress: Progress | None = None,
) -> list[Path]:
    say: Progress = progress or (lambda m: log.info("%s", m))
    if not month:
        today = dt.date.today()
        month = prev_month(today.strftime("%Y-%m")) if today.day < 3 else today.strftime("%Y-%m")
    scope = cfg.get("subscriptions", [{}])[0].get("category", "gr-qc") if len(cfg.get("subscriptions", [])) == 1 else "arXiv"

    say(f"生成月报：{month}（范围 {scope}）")
    data = collect_month(cfg, store, month)
    say(f"  本月论文 {len(data.papers)} 篇，主题 {len(data.theme_stats)} 个")
    if not data.papers:
        say("  该月没有数据，跳过")
        return []

    labels, order = load_theme_taxonomy(cfg.data_dir / "theme_taxonomy.json")
    if use_llm:
        client = LLMClient(cfg, store)
        if client.ready:
            # 先补齐缺失的逐日摘要，否则这些论文会全部落进「其他」
            from .summarize import backfill_summaries

            missing = [p for p in data.papers if not p.summary]
            if missing:
                say(f"  有 {len(missing)} 篇论文缺少摘要，先补齐再汇总")
                taxonomy = load_taxonomy_raw(cfg.data_dir / "theme_taxonomy.json")
                backfill_summaries(
                    client, store, data.papers, taxonomy=taxonomy,
                    lang=cfg.get("report_language", "zh"), progress=say,
                )
                data = collect_month(cfg, store, month)      # 重新分组建模

            target_lang = cfg.get("report_language", "zh")
            say(f"  调用模型 {client.describe()} 生成月度综述"
                + ("（中英对照需两遍）" if target_lang == "zh-en" else ""))
            if target_lang == "zh-en":
                monthly_llm(client, data, lang="zh", labels=labels, order=order, progress=say)
                en_data = collect_month(cfg, store, month)
                monthly_llm(client, en_data, lang="en", labels=labels, order=order, progress=say)
                data.overview = _join(data.overview, en_data.overview)
                for code, note in en_data.theme_notes.items():
                    data.theme_notes[code] = _join(data.theme_notes.get(code, ""), note)
                data.model = f"{client.describe()}（中英对照）"
            else:
                monthly_llm(client, data, lang=target_lang,
                            labels=labels, order=order, progress=say)
                data.model = client.describe()
        else:
            say("  未配置模型密钥，输出统计版月报")

    math = MathService(store, assets_dir=cfg.assets_dir,
                       backend=cfg["report"].get("math_backend", "auto"))
    math.prepare([p.title for p in data.papers])

    missing = coverage_check(cfg.assets_dir, [p.title for p in data.papers])
    if missing:
        say("  警告：有字符不在随仓库分发的字体中：" + " ".join(missing[:10]))

    html_text = render_monthly_html(cfg, data, math, scope_name=scope)
    out_dir = cfg.resolve("reports/monthly")
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{month}.pdf"
    _, backend = render_pdf(html_text, pdf, base_url=cfg.assets_dir)
    say(f"  月报生成（{backend}）：{pdf}")
    store.record_report(month, scope, str(pdf), len(data.papers), kind="monthly")
    return [pdf]
