"""月报：把当月**日报**的重要内嵌再浓缩成一份简报。

设计要点（与日报明确分工）：
  * 输入只取当月的日报摘要（highlights + 主题综述），不再回头翻原始论文；
  * 不列参考文献表，也不列论文清单 —— 正文只标注该内容出现在哪一天的日报上；
  * 篇幅目标 2 页以内：一段概览 + 若干条主要进展 + 分主题要点，简明扼要优先；
  * 统计数字由代码计算，模型只负责文字。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .config import Config
from .llm.client import LLMClient, LLMError
from .models import Paper
from .pdf import render_pdf
from .render import (base_css, font_css, font_preflight, font_stack,
                     load_theme_taxonomy)
from .store import Store

log = logging.getLogger(__name__)
Progress = Callable[[str], None]

PROMPT_VERSION = "monthly-v4"

SYSTEM_ZH = """你是一位相对论与引力领域的科研助理，负责把一个月内每天的 arXiv 日报浓缩成一份月度简报。

硬性规则：
1. 只能依据我提供的日报要点，不得引入外部知识，不得编造。
2. 提到某项工作时，用（10月13日）标注它出现在哪一天的日报上。
   其中**重要**的工作再在日期后面补上它的 arXiv 号，写成（10月13日，arXiv:2610.17034）。
3. arXiv 号只能从我给的「可引用清单」里挑，不得凭记忆写。
   清单里没有的号一律不要写；宁可不写号，也不能写错号。
   每段里附号的工作最多 1–3 篇，只挑最重要的，不要每句都挂号。
4. 只输出 JSON，不要输出解释文字或 Markdown 代码块。

术语与表述：
5. 术语用领域标准译名（准正规模、潮汐形变率、状态方程、铃宕、旋近、并合、屏蔽、
   反作用、慢滚、极端、近极端、无毛等）。拿不准的保留英文原词。
6. 数值、量级、正负号、成立条件不得改动，也不得简化到失真。
7. 逻辑要完整：给结论时给出前提或适用范围；作对比时说明比较对象。
8. 语法从简：短句为主，一句话只说一件事，不用长定语和嵌套从句，不用比喻和夸张。

准确性：
9. 力求准确是第一要求。所有判断都要能在日报里找到依据；不确定就不写。
   宁可写得更长一些把事情说清楚，也不要为了短而牺牲准确性或逻辑完整。

表达：
10. 在准确的前提下力求简明。只写有实质内容的进展，不重复、不铺陈、不写套话。
   能一句话说清的就不要写两句。同一件事只说一遍。

篇幅：
11. 简报面向的是"这个月发生了什么"，不是流水账。分主题要点每个主题只写 2–4 项
    最重要的进展，同类工作合并成一句（例如"多篇工作改进了 X 的约束"），
    不要逐篇罗列，也不要把日报的句子搬过来。宁可少写几项，也不要铺满篇幅。"""

SYSTEM_EN = """You are a research assistant in relativity and gravitation, condensing a month of
daily arXiv digests into a short monthly brief.

Hard rules:
1. Use ONLY the supplied daily-digest notes. Do not add outside knowledge, never invent.
2. When referring to work, mark which day's digest it appeared in, e.g. (Oct 13).
   For **important** items add the arXiv ID after the date: (Oct 13, arXiv:2610.17034).
3. Take arXiv IDs only from the "citable list" I give you, never from memory. If an ID is not in
   the list, omit it — an omitted ID is fine, a wrong ID is not. At most 1-3 IDs per paragraph,
   only for the most important work.
4. Output JSON only, no prose outside it and no Markdown fences.

Terminology and wording:
5. Use standard technical terms. Keep the original wording if unsure.
6. Do not alter or over-simplify numbers, magnitudes, signs, or conditions of validity.
7. Be logically complete: give the premise or scope with any conclusion; name comparison baselines.
8. Keep the grammar simple: short sentences, one idea each, no nesting, no metaphors or hype.

Accuracy:
9. Accuracy comes first. Every statement must be traceable to the supplied digests; if unsure,
   omit it. Prefer a longer, precise account over a short but vague or incomplete one.

Expression:
10. Be concise subject to being accurate. Include only substantive progress; no repetition,
   no padding, no filler. If one sentence suffices, do not write two."""


@dataclass
class MonthlyData:
    month: str
    days: list[str] = field(default_factory=list)            # 有日报的日期（升序）
    digests: dict[str, dict] = field(default_factory=dict)    # day -> 合并后的日报摘要
    theme_counts: dict[str, int] = field(default_factory=dict)
    paper_count: int = 0
    by_category: Counter = field(default_factory=Counter)
    overview: str = ""
    highlights: list[dict] = field(default_factory=list)      # [{text, days:[YYYY-MM-DD]}]
    theme_notes: list[dict] = field(default_factory=list)     # [{code, text, count, days}]
    model: str = ""
    # 每天可引用的论文：{day: [{"id": "2610.17034", "title": "..."}]}
    # 模型只能从这里挑 arXiv 号，生成后还会再校验一次，防止编造。
    paper_index: dict[str, list[dict]] = field(default_factory=dict)
    dropped_ids: int = 0                                     # 被剔除的无效 arXiv 号个数


# ---------------------------------------------------------------------------
# 时间与文本工具
# ---------------------------------------------------------------------------
def month_range(month: str, tz: str = "Asia/Shanghai") -> tuple[str, str]:
    y, m = (int(x) for x in month.split("-"))
    start = dt.date(y, m, 1)
    end = dt.date(y + (m // 12), (m % 12) + 1, 1)
    return start.isoformat(), end.isoformat()


def prev_month(month: str) -> str:
    y, m = (int(x) for x in month.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def target_month(now: dt.datetime | None = None, tz: str = "Asia/Shanghai") -> str:
    """月报目标月份 = 上一个自然月。

    即「北京时间下个自然月的第一天」出上个月的简报；月中手动运行也给上一个
    已经走完的月份。
    """
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else dt.datetime.now(zone)
    return prev_month(now.strftime("%Y-%m"))


def day_label(day: str, lang: str = "zh") -> str:
    """2026-10-07 -> 10月7日 / Oct 7"""
    try:
        d = dt.date.fromisoformat(day)
    except ValueError:
        return day
    if lang == "en":
        return f"{d.strftime('%b')} {d.day}"
    return f"{d.month}月{d.day}日"


def _fmt_days(days: list[str], lang: str) -> str:
    labels = [day_label(d, lang) for d in days if d]
    if not labels:
        return ""
    sep = ", " if lang == "en" else "、"
    return ("(" if lang == "en" else "（") + sep.join(labels) + (")" if lang == "en" else "）")


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def collect_month(cfg: Config, store: Store, month: str) -> MonthlyData:
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    start, end = month_range(month, tz)
    cats = [s["category"] for s in cfg.get("subscriptions", []) if s.get("category")] or None
    rows = store.month_digests(start, end, cats)

    labels, _order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))
    data = MonthlyData(month=month)
    for r in rows:
        r = dict(r)
        # 老版本存档没有主题篇数：从当天该分类的论文标注里直接算出来（不用再调模型）
        if not r.get("theme_counts"):
            r["theme_counts"] = _backfill_theme_counts(store, cfg, labels, tz, r["category"], r["day"])
        if not r.get("papers"):
            r["papers"] = _backfill_papers(store, cfg, tz, r["category"], r["day"])
        day = r["day"]
        if day in data.digests:                     # 同一天多个分类：合并要点
            prev = data.digests[day]
            if r.get("highlights"):
                prev["highlights"] = (str(prev.get("highlights") or "") + "\n" + r["highlights"]).strip()
            themes = prev.setdefault("themes", {})
            for code, note in (r.get("themes") or {}).items():
                old = themes.get(code, "")
                themes[code] = (old + "\n" + note).strip() if old else note
            prev["count"] = int(prev.get("count", 0)) + int(r.get("count", 0))
            tc = prev.setdefault("theme_counts", {})
            for code, n in (r.get("theme_counts") or {}).items():
                tc[code] = int(tc.get(code, 0)) + int(n or 0)
        else:
            data.digests[day] = r
        # 汇总当日可引用的论文（同一天多分类时合并去重）
        bucket = data.paper_index.setdefault(day, [])
        seen = {x.get("id") for x in bucket}
        for it in (r.get("papers") or []):
            pid = bare_id((it or {}).get("id"))
            if pid and pid not in seen:
                bucket.append({"id": pid, "title": str(it.get("title") or "").strip()})
                seen.add(pid)
    data.days = sorted(data.digests)

    # 简报里报"日报实际覆盖的论文数"，而不是库内该月全部论文
    # （用户可能只生成了部分日期的日报，或订阅了多个分类）
    # 按 UTC 时间戳取当月论文：时区只影响月份边界的换算
    papers = store.papers_in_month(tz, month)
    data.by_category = Counter(p.primary_category for p in papers)
    data.paper_count = sum(int(d.get("count") or 0) for d in data.digests.values())

    # 主题篇数同样只统计日报覆盖的论文，否则「其他」会被未生成日报的日期撑大
    counts: Counter = Counter()
    for day in data.days:
        for code, n in (data.digests[day].get("theme_counts") or {}).items():
            counts[code] += int(n or 0)
    for d in data.digests.values():
        for code in (d.get("themes") or {}):
            counts.setdefault(code, 0)
    data.theme_counts = dict(counts)
    return data


def _backfill_theme_counts(store: Store, cfg: Config, labels: dict, tz: str,
                           category: str, day: str) -> dict[str, int]:
    """老存档缺主题篇数时，按该日该分类的论文标注现算一份。"""
    out: Counter = Counter()
    try:
        for p in store.papers_for(category, tz, day, policy=cfg.get("crosslist_policy", "primary")):
            code = next((t for t in (p.themes or []) if t in labels), "other")
            out[code] += 1
    except Exception:  # noqa: BLE001
        pass
    return dict(out)


def _backfill_papers(store: Store, cfg: Config, tz: str, category: str, day: str) -> list[dict]:
    """老存档没有论文清单时，按该日该分类的论文重建，供月报附 arXiv 号。"""
    out: list[dict] = []
    try:
        for p in store.papers_for(category, tz, day, policy=cfg.get("crosslist_policy", "primary")):
            out.append({"id": p.arxiv_id, "title": p.title})
    except Exception:  # noqa: BLE001
        pass
    return out


def day_paper_lines(store: Store, cfg: Config, data: MonthlyData, lang: str,
                    limit: int = 14000) -> str:
    """按日期列出「arXiv 号 + 一句话要点」，供写主要进展时挂号。

    为什么需要它：时间线与主题材料都是日报改写过的**散文**，里面没有标题也没有号；
    模型无法把"某篇工作"对应到可引用清单里的英文标题，于是干脆不挂号（实测为 0）。
    这里直接给出「号 ↔ 要点」的对照，模型照抄即可。
    """
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    policy = cfg.get("crosslist_policy", "primary")
    cats = [x["category"] for x in cfg.get("subscriptions", []) if x.get("category")]
    out: list[str] = []
    used = 0
    for day in data.days:
        if used >= limit:
            break
        seen: set[str] = set()
        lines: list[str] = []
        for cat in (cats or [None]):
            try:
                papers = store.papers_for(cat, tz, day, policy=policy) if cat else []
            except Exception:  # noqa: BLE001
                continue
            for p in papers:
                aid = bare_id(p.arxiv_id)
                if aid in seen:
                    continue
                seen.add(aid)
                note = " ".join((p.summary or "").split())
                if not note:
                    continue
                if len(note) > 150:
                    note = note[:150] + "…"
                lines.append(f"  {aid}  {note}")
        if lines:
            block = f"{day_label(day, lang)}：\n" + "\n".join(lines)
            used += len(block)
            out.append(block)
    return "\n".join(out)


def digest_timeline(data: MonthlyData, labels: dict, order: list[str], lang: str) -> tuple[str, str]:
    """给模型的两段素材：按天的时间线 + 按主题聚合的日报综述。"""
    lines = []
    for day in data.days:
        d = data.digests.get(day) or {}
        hl = " ".join(str(d.get("highlights") or "").split())
        if hl:
            lines.append(f"{day_label(day, lang)}：{hl}")
    timeline = "\n".join(lines)

    by_theme: dict[str, list[str]] = defaultdict(list)
    for day in data.days:
        d = data.digests.get(day) or {}
        for code, note in (d.get("themes") or {}).items():
            if str(note).strip():
                by_theme[code].append(f"{day_label(day, lang)}：{' '.join(str(note).split())}")
    blocks = []
    for code in list(order) + [c for c in by_theme if c not in order]:
        if code in by_theme:
            label = labels.get(code, (code, code))[0 if lang == "zh" else 1]
            blocks.append(f"## {label}（代码 {code}）\n" + "\n".join(by_theme[code]))
    return timeline, "\n\n".join(blocks)


def _chunk(lines: list[str], max_chars: int = 24000, max_items: int = 40) -> list[list[str]]:
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


def _valid_days(raw, month: str) -> list[str]:
    """把模型返回的 MM-DD / YYYY-MM-DD 校验成该月内真实存在的日期。"""
    out: list[str] = []
    for item in raw or []:
        s = str(item).strip()
        if not s:
            continue
        if len(s) == 5 and s[2] == "-":
            s = f"{month[:4]}-{s}"
        try:
            d = dt.date.fromisoformat(s)
        except ValueError:
            continue
        if d.strftime("%Y-%m") == month and s not in out:
            out.append(s)
    return sorted(out)


_CITE_GROUP = re.compile(r"[（(]([^）)]*)[）)]")
_VERSION = re.compile(r"v\d+$")


def bare_id(value: str) -> str:
    """去掉版本号：2610.17034v1 -> 2610.17034。

    论文的唯一标识带版本号（uid），但正文里引用时按惯例只写裸号，
    两边必须归一化后再比较，否则一个都匹配不上。
    """
    return _VERSION.sub("", str(value or "").strip())
_DAY_IN_TEXT = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_ARXIV_IN_TEXT = re.compile(r"arXiv:\s*([0-9]{4}\.[0-9]{4,5})", re.I)


def _citation_days(blob: str, month: str) -> list[str]:
    out: list[str] = []
    for m, d in _DAY_IN_TEXT.findall(blob):
        try:
            day = dt.date(int(month[:4]), int(m), int(d))
        except ValueError:
            continue
        s = day.isoformat()
        if s.startswith(month) and s not in out:
            out.append(s)
    return out


def sanitize_citations(text: str, month: str, index: dict[str, list[dict]]) -> tuple[str, int]:
    """校验正文里的 arXiv 号，只保留确实出现在所标日期日报里的那些。

    模型被要求"只能从给定清单里挑号"，但仍可能编造或张冠李戴；这里做最后一道闸：
      * 括号里必须同时有日期与 arXiv 号（号必须挂在某一天上）；
      * arXiv 号必须出现在该括号标注的某一天的论文清单里；
      * 任何不满足的号一律删掉，只留下日期。
    返回（清洗后的文本, 被剔除的个数）。
    """
    if not text:
        return text, 0
    dropped = 0

    def fix(m: re.Match) -> str:
        nonlocal dropped
        inner = m.group(1)
        ids = _ARXIV_IN_TEXT.findall(inner)
        if not ids:
            return m.group(0)
        days = _citation_days(inner, month)
        allowed: set[str] = set()
        for d in days:
            for it in index.get(d, []):
                allowed.add(bare_id(it.get("id")))
        keep = [i for i in ids if bare_id(i) in allowed]
        dropped += len(ids) - len(keep)
        # 抹掉所有 arXiv 号，再把合法的按原顺序放回去
        cleaned = _ARXIV_IN_TEXT.sub("", inner)
        cleaned = re.sub(r"[，,、;；]\s*(?=[，,、;；]|$)", "", cleaned)
        cleaned = re.sub(r"[，,、;；]\s*$", "", cleaned).strip()
        if keep:
            cleaned = (cleaned + "，" if cleaned else "") + "，".join(f"arXiv:{i}" for i in keep)
        return "（" + cleaned + "）" if cleaned else ""

    out = _CITE_GROUP.sub(fix, text)
    return re.sub(r"\s{2,}", " ", out).strip(), dropped


def pick_highlight_ids(raw_ids, days: list[str], index: dict[str, list[dict]]) -> tuple[list[str], int]:
    """校验主要进展的 arxiv 字段：只保留确实出现在所标日期清单里的号。

    返回（保留的号, 剔除的个数）。日期是独立字段，所以这里按 days 而不是按正文括号判断。
    """
    allowed: set[str] = set()
    for d in days:
        for it in index.get(d, []):
            allowed.add(bare_id(it.get("id")))
    picked: list[str] = []
    dropped = 0
    for raw in (raw_ids or []):
        aid = bare_id(re.sub(r"^arXiv:\s*", "", str(raw), flags=re.I))
        if not aid:
            continue
        if aid in allowed and aid not in picked:
            picked.append(aid)
        else:
            dropped += 1
    return picked, dropped


def _allowed_ids_text(index: dict[str, list[dict]], month: str, lang: str,
                      days: list[str] | None = None) -> str:
    """给模型的可引用清单：按日期列出「arXiv 号 + 标题」。

    **必须带标题**：只给号的话，模型无法判断哪个号对应哪项工作，
    结果就是一个号都不敢挂（实测只能挂出 1 个）。带上标题它才能对上号。
    """
    lines = []
    for day in sorted(days if days is not None else index):
        items = index.get(day) or []
        if not items:
            continue
        lines.append(f"{day_label(day, lang)}：")
        for x in items:
            title = " ".join(str(x.get("title") or "").split())
            if len(title) > 110:
                title = title[:110] + "…"
            lines.append(f"  {bare_id(x.get('id'))}  {title}")
    return "\n".join(lines)


def monthly_llm(
    client: LLMClient, data: MonthlyData, *, lang: str, labels: dict, order: list[str],
    progress: Progress, store: Store | None = None, cfg: Config | None = None,
) -> None:
    """两步：先分主题要点（map，必要时分块合并），再总览与主要进展（reduce）。"""
    timeline, theme_material = digest_timeline(data, labels, order, lang)
    id_list = _allowed_ids_text(data.paper_index, data.month, lang) or "（本月无可引用的 arXiv 号）"

    def ids_for(text) -> str:
        """只给这一批材料里出现的日期对应的论文清单，避免把整月都塞进去。"""
        if not isinstance(text, str):
            text = "\n".join(str(x) for x in text)
        days = _citation_days(text, data.month)
        return _allowed_ids_text(data.paper_index, data.month, lang, days=days) \
            or "（这批材料涉及日期暂无可引用的 arXiv 号）"
    schema_themes = '{"themes":[{"code":"...","text":"...","days":["10-07"],"count":0}]}'
    schema_final = ('{"overview":"...",'
                    '"highlights":[{"text":"...","days":["10-13"],"arxiv":["2610.17034"]}]}')

    # ---- map：分主题要点 ----
    raw_items: list[str] = []
    if theme_material:
        blocks = _chunk(theme_material.split("\n\n"))
        for i, blk in enumerate(blocks, 1):
            tag = f"（第 {i}/{len(blocks)} 批）" if len(blocks) > 1 else ""
            progress(f"  月报 map：整理分主题要点{tag}")
            block_text = "\n".join(str(x) for x in blk) if not isinstance(blk, str) else blk
            ask = (
                '请输出 JSON：{"themes":[{"code":"主题代码","text":"该主题要点，句末用（10月7日）'
                '标注它出现在哪天的日报上。其中最重要的 1–3 项工作必须补上 arXiv 号，写成'
                '（10月7日，arXiv:2610.12345）；号与标题的对应关系以上面的可引用清单为准，'
                '清单里没有的号不要写。每个主题只写最重要的进展，不要罗列，不要复述日报。",'
                '"days":["10-07"],"count":0}]}，code 必须用我给的代码。'
                if lang == "zh"
                else 'Return JSON: {"themes":[{"code":"...","text":"... cite the day as (Oct 7), '
                     'adding an arXiv ID for important work; IDs must come from the citable list",'
                     '"days":["10-07"],"count":0}]}, reusing my codes.'
            )
            user = (
                f"月份：{data.month}\n\n以下是本月各主题的**日报**要点（按主题聚合）：\n\n"
                f"{block_text}\n\n"
                f"可引用清单（号 + 标题；重要工作只能从这里挑号）：\n{ids_for(block_text)}\n\n{ask}"
            )
            try:
                obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                                       schema_hint=schema_themes, max_tokens=8000)
                for item in obj.get("themes") or []:
                    if isinstance(item, dict) and str(item.get("text") or "").strip():
                        raw_items.append(json.dumps(item, ensure_ascii=False))
            except LLMError as exc:
                progress(f"  第 {i} 批主题要点失败：{exc}")

        if len(blocks) > 1 and len(raw_items) > 1:
            progress("  月报 merge：合并分主题要点")
            user = (
                f"月份：{data.month}\n\n以下是分批整理的主题要点（每行一个 JSON），请合并去重，"
                f"同一 code 只保留一条，保留其中的日期与 arXiv 号，篇幅仍要短：\n\n"
                + "\n".join(raw_items) + f"\n\n请输出 JSON：{schema_themes}"
            )
            try:
                obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                                       schema_hint=schema_themes, max_tokens=8000)
                merged = [json.dumps(it, ensure_ascii=False) for it in (obj.get("themes") or [])
                          if isinstance(it, dict) and str(it.get("text") or "").strip()]
                if merged:
                    raw_items = merged
            except LLMError as exc:
                progress(f"  合并失败，保留分批结果：{exc}")

    themes: list[dict] = []
    for raw in raw_items:
        try:
            item = json.loads(raw)
        except Exception:
            continue
        code = str(item.get("code") or "other")
        if code not in labels:
            code = "other"
        themes.append({
            "code": code,
            "text": " ".join(str(item.get("text") or "").split()),
            "days": _valid_days(item.get("days"), data.month),
            "count": data.theme_counts.get(code, int(item.get("count") or 0)),
        })
    themes.sort(key=lambda x: (-(x["count"] or 0),
                               order.index(x["code"]) if x["code"] in order else 999))
    # 保留所有确有论文的主题（不设条数上限）
    data.theme_notes = [t for t in themes if t["text"] and (t["count"] or 0) > 0]
    # 最后一道闸：剔除编造或张冠李戴的 arXiv 号，只保留日期
    dropped = 0
    for t in data.theme_notes:
        t["text"], n = sanitize_citations(t["text"], data.month, data.paper_index)
        dropped += n
    data.dropped_ids += dropped

    # ---- reduce：总览 + 主要进展 ----
    if data.dropped_ids:
        progress(f"  校验剔除 {data.dropped_ids} 个无法归属到所标日期的 arXiv 号")
    progress("  月报 reduce：生成总览与主要进展")
    stats = (
        f"本月有日报 {len(data.days)} 天；新论文 {data.paper_count} 篇；"
        f"主题分布：{dict(sorted(data.theme_counts.items(), key=lambda kv: -kv[1])[:10])}"
    )
    themes_brief = "\n".join(
        f"- {labels.get(t['code'], (t['code'], t['code']))[0 if lang == 'zh' else 1]}：{t['text']}"
        for t in data.theme_notes
    )
    ask = (
        '请输出 JSON：{"overview":"本月概览，写成一段连贯文字，像论文摘要那样先总述本月主线，'
        '再点出最突出的方向以及它们之间的关系。力求准确，不要为了短而省略关键条件或因果关系；'
        '也不要罗列篇数、不要逐条复述日报。提到具体工作时在句末用（10月13日）标注它出现在哪天的日报上，'
        '重要工作再补上 arXiv 号，写成（10月13日，arXiv:2610.17034），号必须来自可引用清单。",'
        '"highlights":[{"text":"一条主要进展，1–2 句，简洁准确",'
        '"days":["10-13"],"arxiv":["2610.17034"]}]}，highlights 给 4–6 条。'
        '日期只填 days 字段、不要写进 text；'
        '每条 highlights 都必须在 arxiv 字段里给出它对应的 arXiv 号（1–2 个，'
        '依据"当日论文对照表"按内容匹配），这是重要文献的主要落点。'
        '只有确实匹配不上时才留空数组。'
        if lang == "zh"
        else 'Return JSON: {"overview":"A coherent paragraph overview, like an abstract; be accurate '
             'rather than terse; cite days in parentheses, adding an arXiv ID from the citable list '
             'for important work",'
             '"highlights":[{"text":"...","days":["10-13"],"arxiv":["2610.17034"]}]} '
             'with 4-6 highlights. Put the date only in the days field, not in text; '
             'every highlight must carry its arXiv IDs in the arxiv field, matched by content '
             'against the per-day paper table. Use an empty array only if nothing matches.'
    )
    paper_lines = day_paper_lines(store, cfg, data, lang) if (store and cfg) else ""
    user = (
        f"月份：{data.month}\n统计：{stats}\n\n"
        f"本月各天日报的要点时间线：\n{timeline}\n\n"
        f"各主题要点：\n{themes_brief}\n\n"
        f"可引用清单（号 + 标题；重要工作只能从这里挑号）：\n{id_list}\n\n"
        + (f"当日论文对照表（号 + 一句话要点，写 highlights 时据此挂号）：\n{paper_lines}\n\n"
           if paper_lines else "")
        + ask
    )
    try:
        obj = client.chat_json(SYSTEM_ZH if lang == "zh" else SYSTEM_EN, user,
                               schema_hint=schema_final, max_tokens=2600)
        data.overview = str(obj.get("overview") or "").strip()
        data.highlights = []
        for h in (obj.get("highlights") or []):
            if not (isinstance(h, dict) and str(h.get("text") or "").strip()):
                continue
            days = _valid_days(h.get("days"), data.month)
            picked, ndrop = pick_highlight_ids(h.get("arxiv"), days, data.paper_index)
            data.dropped_ids += ndrop
            text, n = sanitize_citations(str(h["text"]).strip(), data.month, data.paper_index)
            data.dropped_ids += n
            data.highlights.append({"text": text, "days": days, "arxiv": picked})
        data.overview, n = sanitize_citations(data.overview, data.month, data.paper_index)
        data.dropped_ids += n
    except LLMError as exc:
        progress(f"  总览生成失败：{exc}")


# ---------------------------------------------------------------------------
# 渲染
# ---------------------------------------------------------------------------
def _last_day(month: str) -> int:
    y, m = (int(x) for x in month.split("-"))
    nxt = dt.date(y + (m // 12), (m % 12) + 1, 1)
    return (nxt - dt.timedelta(days=1)).day


def linkify_arxiv(text: str) -> str:
    """转义后把正文里的 arXiv 号变成可点击链接。"""
    safe = html.escape(text or "")
    return re.sub(
        r"arXiv:\s*([0-9]{4}\.[0-9]{4,5})",
        lambda m: (f'<a class="arx" href="https://arxiv.org/abs/{m.group(1)}">'
                   f'arXiv:{m.group(1)}</a>'),
        safe,
    )


def render_monthly_html(cfg: Config, data: MonthlyData, *, scope_name: str) -> str:
    lang = cfg.get("report_language", "zh")
    bilingual = lang == "zh-en"
    if lang not in ("zh", "en"):
        lang = "zh"
    labels, _order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    generated = dt.datetime.now(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")
    y, m = data.month.split("-")

    def h2(zh: str, en: str) -> str:
        return "<h2>" + (f"{zh} / {en}" if bilingual else (zh if lang == "zh" else en)) + "</h2>"

    title = f"{scope_name} 月度简报" if lang == "zh" else f"{scope_name} Monthly Brief"
    if bilingual:
        title = f"{scope_name} 月度简报 / Monthly Brief"
    # 覆盖区间取日报实际覆盖的日期，而不是整个自然月（可能只跑了一部分）
    first, last = (data.days[0], data.days[-1]) if data.days else (data.month + "-01", data.month + "-01")
    if lang == "zh":
        span = (f"{day_label(first)}–{day_label(last)}" if first != last
                else day_label(first))
        subtitle = (f"{y} 年 {int(m)} 月 · 覆盖 {span} · {len(data.days)} 天日报 · "
                    f"论文 {data.paper_count} 篇 · 生成于 {generated}")
    else:
        span = f"{day_label(first, 'en')}–{day_label(last, 'en')}" if first != last else day_label(first, "en")
        subtitle = (f"{y}-{m} · covering {span} · {len(data.days)} daily digests · "
                    f"{data.paper_count} papers · generated {generated}")

    parts = ["<!doctype html><html><head><meta charset='utf-8'>"]
    parts.append(f"<title>{html.escape(title)}</title><style>")
    parts.append(font_css(cfg))
    parts.append(base_css(f"{scope_name} · {data.month}", "", font_stack(cfg)))
    parts.append("""
.abstract { background:#f6f8fb; border-left:3px solid #6f9fdc; padding:3.5mm 4.5mm; margin-bottom:5mm; }
.abstract p { margin:0 0 1.8mm; }
.abstract p:last-child { margin:0; }
ol.moves { margin:0 0 2mm 5.5mm; padding:0; }
ol.moves li { margin-bottom:1.8mm; }
.theme { margin-bottom:2.6mm; break-inside:avoid; }
.theme .name { font-weight:700; font-size:11pt; color:#22314a; }
.theme .meta { color:#7a8698; font-size:9pt; font-weight:400; }
.theme p { margin:1mm 0 0; }
.daytag { color:#55606f; font-size:9.5pt; }
a.arx { color:#2f6fb5; text-decoration:none; font-size:9.5pt; }
</style></head><body>""")
    parts.append(f"<h1>{html.escape(title)}</h1>")
    parts.append(f"<div class='sub'>{html.escape(subtitle)}</div>")

    if not data.days:
        parts.append("<p>" + ("该月没有可用的日报摘要，请先生成当月日报。"
                              if lang == "zh" else "No daily digests found for this month.") + "</p>")

    if data.overview:
        parts.append(h2("本月概览", "Overview"))
        parts.append("<div class='abstract'>")
        for para in data.overview.splitlines():
            if para.strip():
                parts.append(f"<p>{linkify_arxiv(para.strip())}</p>")
        parts.append("</div>")

    if data.highlights:
        parts.append(h2("主要进展", "Highlights"))
        parts.append("<ol class='moves'>")
        for h in data.highlights:
            # _fmt_days 已带括号，这里要去掉外层括号再拼号，避免出现（（…））
            tag = _fmt_days(h.get("days") or [], lang).strip("()（）")
            ids = h.get("arxiv") or []
            if ids:
                links = "，".join(
                    f'<a class="arx" href="https://arxiv.org/abs/{html.escape(a)}">arXiv:{html.escape(a)}</a>'
                    for a in ids
                )
                tag = (f"（{tag}，{links}）" if tag else f"（{links}）")
            elif tag:
                tag = f"（{tag}）"
            suffix = f" <span class='daytag'>{tag}</span>" if tag else ""
            parts.append(f"<li>{linkify_arxiv(h['text'])}{suffix}</li>")
        parts.append("</ol>")

    if data.theme_notes:
        parts.append(h2("分主题要点", "By theme"))
        for t in data.theme_notes:
            label = labels.get(t["code"], (t["code"], t["code"]))[0 if lang == "zh" else 1]
            # 日期已在正文里逐条标注，这里只给篇数，避免同一信息出现两次
            meta_html = ""
            if t.get("count"):
                meta_html = (f" <span class='meta'>{t['count']} 篇</span>" if lang == "zh"
                             else f" <span class='meta'>{t['count']} papers</span>")
            parts.append(
                f"<div class='theme'><span class='name'>{html.escape(label)}</span>{meta_html}"
                f"<p>{linkify_arxiv(t['text'])}</p></div>"
            )

    foot = ("内容取自当月每日日报，不另列文献；括号内标注该内容出现的日报日期，"
            "重要工作附 arXiv 号（可点击，已逐条核对确属该日）。"
            if lang == "zh" else
            "Condensed from the daily digests; parentheses give the digest date, and important "
            "work carries a clickable arXiv ID.")
    if data.model:
        foot += ("　摘编模型：" if lang == "zh" else "  Model: ") + data.model
    parts.append(f"<div class='footer'>{html.escape(foot)}</div>")
    parts.append("</body></html>")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
def should_run_monthly(cfg: Config, now: dt.datetime | None = None) -> str | None:
    """北京时间每月 1 号生成上一个自然月的简报；不需要则返回 None。"""
    if not cfg["schedule"].get("auto_monthly", True):
        return None
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    zone = ZoneInfo(tz)
    now = now.astimezone(zone) if now else dt.datetime.now(zone)
    if now.day != 1:
        return None
    return prev_month(now.strftime("%Y-%m"))


def run_monthly(
    cfg: Config, store: Store, *, month: str | None = None,
    use_llm: bool = True, progress: Progress | None = None,
) -> list[Path]:
    say: Progress = progress or (lambda m: log.info("%s", m))
    tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
    if not month:
        month = target_month(tz=tz)
    subs = [x for x in cfg.get("subscriptions", []) if x.get("enabled", True)]
    single = subs[0].get("category", "gr-qc") if len(subs) == 1 else "arXiv"
    # 月报按分类分目录，避免 reports/monthly/ 越堆越乱：
    #   reports/monthly/gr-qc/2026-10.pdf
    #   reports/monthly/arXiv/2026-10.pdf   （订阅多个分类时的合并简报）
    scope_slug = re.sub(r"[^A-Za-z0-9._-]", "_", str(single)) or "arXiv"
    scope = single
    lang = cfg.get("report_language", "zh")

    say(f"生成月报：{month}（范围 {scope}，来源：该月日报）")
    data = collect_month(cfg, store, month)
    say(f"  该月有日报 {len(data.days)} 天，论文 {data.paper_count} 篇，主题 {len(data.theme_counts)} 个")
    if not data.days:
        say("  该月没有日报摘要，跳过（请先用 run.py daily 生成当月日报）")
        return []

    labels, order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))
    if use_llm:
        client = LLMClient(cfg, store)
        if client.ready:
            say(f"  调用模型 {client.describe()} 浓缩月度简报"
                + ("（中英对照需两遍）" if lang == "zh-en" else ""))
            if lang == "zh-en":
                monthly_llm(client, data, lang="zh", labels=labels, order=order, progress=say,
                            store=store, cfg=cfg)
                en_data = collect_month(cfg, store, month)
                monthly_llm(client, en_data, lang="en", labels=labels, order=order, progress=say,
                            store=store, cfg=cfg)
                data.overview = _join(data.overview, en_data.overview)
                data.model = f"{client.describe()}（中英对照）"
            else:
                monthly_llm(client, data, lang=lang, labels=labels, order=order, progress=say,
                            store=store, cfg=cfg)
                data.model = client.describe()
        else:
            say("  未配置模型密钥，只出统计版简报")
    else:
        say("  已跳过模型（--no-llm），只出统计版简报")

    for w in font_preflight(cfg):
        say("  字体提醒：" + w)

    html_text = render_monthly_html(cfg, data, scope_name=scope)
    # 跟随 paths.reports（而不是写死 reports/），自定义报告目录时月报也一起搬过去
    out_dir = cfg.reports_dir / "monthly" / scope_slug
    out_dir.mkdir(parents=True, exist_ok=True)
    pdf = out_dir / f"{month}.pdf"
    _, backend = render_pdf(html_text, pdf, base_url=cfg.assets_dir)
    say(f"  月报生成（{backend}）：{pdf}")
    store.record_report(month, scope, str(pdf), data.paper_count, kind="monthly")
    return [pdf]


def _join(a: str, b: str) -> str:
    a, b = (a or "").strip(), (b or "").strip()
    return f"{a}\n\n{b}" if a and b else (a or b)
