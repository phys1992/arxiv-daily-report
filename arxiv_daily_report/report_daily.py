"""日报编排：抓取 -> 摘要 -> 渲染 PDF -> 记录索引。"""
from __future__ import annotations

import datetime as dt
import json
import logging
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

from .collect import collect, enabled_categories, target_day
from .config import Config
from .llm.client import LLMClient
from .mathrender import MathService
from .models import Paper
from .pdf import render_pdf
from .fulltext import FullText, FullTextService
from .render import (ReportContext, font_css, font_preflight, font_stack,
                     load_theme_taxonomy, render_daily_html)
from .store import Store
from .summarize import apply_summary, review_day, summarize_day

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def load_taxonomy_raw(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("themes", [])
    except Exception:  # noqa: BLE001
        return [{"code": "other", "zh": "其他", "en": "Other"}]



def _out_dir(cfg: Config, sub: dict) -> Path:
    """分类报告的**根**目录；具体某一天再按月分一层（见 build_category_report）。

    长期运行下 `reports/gr-qc/` 会堆几百个 PDF，按月分层后：
        reports/gr-qc/2026-10/2026-10-13.pdf
        reports/gr-qc/2026-11/2026-11-01.pdf
        reports/gr-qc/latest.pdf          <- 最新一期，路径固定不变
    """
    custom = (sub.get("out_dir") or "").strip()
    if custom:
        return cfg.resolve(custom)
    # 默认落在 paths.reports 下（而不是写死 reports/），这样自定义报告目录对日报同样生效
    return cfg.reports_dir / sub["category"]


def _category_name(cfg: Config, category: str) -> str:
    if category == "combined":
        return "arXiv"
    try:
        data = json.loads((cfg.data_dir / "arxiv_categories.json").read_text(encoding="utf-8"))
        name = data.get(category)
        if isinstance(name, str) and name:
            return f"{category} — {name}"
    except Exception:  # noqa: BLE001
        pass
    return category


def build_category_report(
    cfg: Config,
    store: Store,
    *,
    category: str,
    day: str,
    papers: list[Paper],
    math: MathService,
    use_llm: bool,
    out_dir: Path,
    report_name: str = "",
    progress: Progress | None = None,
    extra: dict | None = None,
) -> tuple[Path, str]:
    say: Progress = progress or (lambda m: log.info("%s", m))
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")
    lang = cfg.get("report_language", "zh")
    if lang == "zh-en":
        lang = "zh"           # 正文以中文为主，小标题由 render 追加英文

    taxonomy = load_taxonomy_raw(cfg.data_asset("theme_taxonomy.json"))
    labels, order = load_theme_taxonomy(cfg.data_asset("theme_taxonomy.json"))

    highlights, theme_notes, model = "", {}, ""
    bilingual = cfg.get("report_language", "zh") == "zh-en"

    # ---- 复用：这一天的论文都已经有当前版本的要点，就不要再花一次模型钱 ----
    # 之前 summarized 标志只写不读，导致每次重跑都把全部论文重新送给模型；
    # 现在按「要点是否存在 + 提示词版本是否一致 + 当天摘要存档是否在」判断。
    from .summarize import PROMPT_VERSION as _PV

    def _reusable(p) -> bool:
        return bool((p.summary or "").strip()) and getattr(p, "llm_prompt_version", "") == _PV

    reused = False
    digest = store.get_day_digest(day, category) if papers else None
    if use_llm and papers and digest and all(_reusable(p) for p in papers):
        highlights = str(digest.get("highlights") or "")
        theme_notes = dict(digest.get("themes") or {})
        model = str(digest.get("model") or "")
        reused = True
        say(f"  {len(papers)} 篇均已有当前版本要点，复用存档，不调用模型"
            + (f"（{model}）" if model else ""))

    # 全文：抓取 arXiv HTML 并提取「结论/讨论」小节，供模型结合摘要一起读
    fulltexts: dict = {}
    ft_stats: dict = {}
    if use_llm and papers and not reused:
        service = FullTextService(store, cfg, progress=say)
        if service.enabled:
            try:
                service.prepare([p.uid for p in papers])
            except Exception as exc:  # noqa: BLE001
                log.warning("全文抓取中断（继续用摘要）：%s", exc)
            for p in papers:
                row = store.get_fulltext(p.uid)
                if row is not None:
                    ft = FullText.from_row(row)
                    if ft.has_fulltext:
                        fulltexts[p.uid] = ft
            ft_stats = dict(service.stats)
            n_full = len(fulltexts)
            n_weak = sum(1 for ft in fulltexts.values() if ft.weak_section)
            extra = []
            if n_weak:
                extra.append(f"其中 {n_weak} 篇命中「引言兼小结」")
            if n_full < len(papers):
                extra.append(f"{len(papers) - n_full} 篇仅摘要")
            say(f"  全文就绪：{n_full}/{len(papers)} 篇含结论/讨论节"
                + (f"（{'；'.join(extra)}）" if extra else ""))
            # 结论原文只作为模型输入，不直接进 PDF，因此无需预渲染其中的公式
        else:
            say("  全文抓取已在配置中关闭，仅用摘要")

    if use_llm and papers and not reused:
        client = LLMClient(cfg, store)
        if client.ready:
            say(
                f"  调用模型 {client.describe()} 生成摘要（{len(papers)} 篇"
                + ("，中英对照需两遍）…" if bilingual else "）…")
            )
            ft_chars = int((cfg.get("fulltext", {}) or {}).get("max_chars", 4000) or 4000)
            if bilingual:
                from .summarize import summarize_day_bilingual

                summary = summarize_day_bilingual(
                    client, papers, category=category, day=day, taxonomy=taxonomy,
                    fulltexts=fulltexts, fulltext_chars=ft_chars,
                )
            else:
                summary = summarize_day(
                    client, papers, category=category, day=day, taxonomy=taxonomy, lang=lang,
                    fulltexts=fulltexts, fulltext_chars=ft_chars,
                )
            if summary.ok:
                apply_summary(papers, summary)
                highlights, theme_notes, model = summary.highlights, summary.theme_notes, summary.model
                for p in papers:
                    store.save_paper_summary(p)
            else:
                say(f"  模型摘要失败（{summary.error}），降级为元数据版")
        else:
            say("  未配置模型密钥，输出元数据版")

    math.prepare([p.title for p in papers] + [p.summary for p in papers])

    # 字体预检：字体由系统提供，这里只提醒「系统有没有中文字体」
    for w in font_preflight(cfg):
        say("  字体提醒：" + w)

    # 存档当日要点与主题综述（外加每个主题的篇数）：月报只读这些日报摘要
    from collections import Counter as _Counter

    theme_counts = _Counter()
    for _p in papers:
        _code = next((t for t in (_p.themes or []) if t in labels), "other")
        theme_counts[_code] += 1
    try:
        store.save_day_digest(
            day, category,
            highlights=highlights,
            themes=theme_notes,
            theme_counts=dict(theme_counts),
            papers=[{"id": p.arxiv_id, "title": p.title} for p in papers],
            model=model,
            count=len(papers),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("存档日报摘要失败（不影响出报告）：%s", exc)

    ctx = ReportContext(
        category=category,
        category_name=report_name or _category_name(cfg, category),
        day=day,
        papers=papers,
        assets_dir=cfg.assets_dir,
        report_language=lang,
        timezone=tz_name,
        generated_at=dt.datetime.now(ZoneInfo(tz_name)).strftime("%Y-%m-%d %H:%M %Z"),
        highlights=highlights,
        theme_notes=theme_notes,
        theme_labels=labels,
        theme_order=order,
        llm_model=model,
        late_additions=int((extra or {}).get("late_additions", 0)),
        bilingual=(cfg.get("report_language", "zh") == "zh-en"),
        font_css_text=font_css(cfg),
        font_stack_text=font_stack(cfg),
        notice=str((extra or {}).get("notice", "")),
    )
    html_text = render_daily_html(ctx, math)

    # 输出目录按月分一层，避免单目录文件过多；latest.pdf 仍放在分类根目录
    month_dir = out_dir / day[:7]          # YYYY-MM
    month_dir.mkdir(parents=True, exist_ok=True)
    if cfg["report"].get("keep_html"):
        (month_dir / f"{day}.html").write_text(html_text, encoding="utf-8")
    pdf_path = month_dir / f"{day}.pdf"
    _, backend = render_pdf(html_text, pdf_path, base_url=cfg.assets_dir)
    say(f"  PDF 生成（{backend}）：{pdf_path}")

    # 最新一期固定路径，便于反复打开同一个文件
    latest = out_dir / "latest.pdf"
    try:
        latest.write_bytes(pdf_path.read_bytes())
    except Exception as exc:  # noqa: BLE001
        log.warning("写入 latest.pdf 失败：%s", exc)

    publish_to(cfg, pdf_path)
    return pdf_path, backend


def publish_to(cfg: Config, pdf_path: Path) -> None:
    dest_root = cfg.publish_dir
    if not dest_root:
        return
    try:
        rel = pdf_path.relative_to(cfg.reports_dir)
    except ValueError:
        rel = Path(pdf_path.name)
    dest = dest_root / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(pdf_path.read_bytes())
    log.info("已发布到 %s", dest)


def write_index(cfg: Config, store: Store, day: str) -> Path:
    out = cfg.reports_dir / "index.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    rows = store.list_reports(kind="daily", limit=400)
    lines = [
        "# arXiv 论文日报索引",
        "",
        f"最近更新：{dt.datetime.now().strftime('%Y-%m-%d %H:%M')}",
        "",
        "| 日期（北京） | 分类 | 篇数 | 版本 | 文件 |",
        "|---|---|---|---|---|",
    ]
    for r in rows:
        try:
            rel = Path(r["path"]).relative_to(cfg.reports_dir)
        except ValueError:
            rel = Path(r["path"]).name
        lines.append(
            f"| {r['day']} | {r['category']} | {r['count']} | v{r['revision']} | [{rel}]({rel}) |"
        )
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


# ---------------------------------------------------------------------------
# 补缺：运行前扫一遍最近若干天，把「有论文但还没生成报告」的日期补上
# ---------------------------------------------------------------------------
def _day_list(start: dt.date, end: dt.date) -> list[str]:
    out, d = [], start
    while d <= end:
        out.append(d.isoformat())
        d += dt.timedelta(days=1)
    return out


def _auto_lookback(cfg: Config, store: Store, day: str) -> int:
    """抓取窗口：至少 catch_up_days；停机多久就补多久（上限 backfill_days）。

    否则停机 20 天后，最早那十几天根本没抓过论文数据，补缺也就无从补起。
    """
    sched = cfg["schedule"]
    base = max(1, int(sched.get("catch_up_days", 7)))
    cap = int(sched.get("backfill_days", 31))
    if cap <= 0:
        return base
    last = store.kv_get("last_success_day", "")
    if not last:
        # 首次运行：把「当月」整段都抓下来，否则月初那几天没有论文数据，
        # 后面的补缺也就补不出来。
        try:
            target = dt.date.fromisoformat(day)
        except ValueError:
            return base
        since_month = (target - target.replace(day=1)).days + 1
        return min(max(base, since_month), max(base, cap))
    try:
        gap = (dt.date.fromisoformat(day) - dt.date.fromisoformat(last)).days
    except ValueError:
        return base
    if gap <= 0:
        return base
    return min(max(base, gap + 1), max(base, cap))


def missing_days(cfg: Config, store: Store, *, today: str | None = None,
                 lookback: int | None = None) -> list[tuple[str, str]]:
    """找出「有论文、但还没有日报」的 (日期, 分类)。

    只看 [昨天 - lookback + 1, 昨天]（默认覆盖当月），**不看今天**——今天的
    arXiv 公告还没结束。没有论文的日期不算缺失（当天本来就没有可报告的论文）。
    """
    sched = cfg["schedule"]
    tz_name = sched.get("timezone", "Asia/Shanghai")
    days_back = int(lookback if lookback is not None else sched.get("backfill_days", 31))
    if days_back <= 0:
        return []

    now = dt.datetime.now(ZoneInfo(tz_name))
    if today:
        last = dt.date.fromisoformat(today) - dt.timedelta(days=1)
    else:
        last = (now.astimezone(ZoneInfo(tz_name)).date() - dt.timedelta(days=1))
    first = last - dt.timedelta(days=days_back - 1)
    # 不早于本月 1 号；月初时自然回看到上个月，避免跨月停机漏掉
    month_start = last.replace(day=1)
    if month_start > first:
        first = max(first, month_start)
    # 范围就是「当月 1 号 ~ 昨天」：首次运行会把整月补齐，之后每次续补。
    first = max(first, month_start)

    cats = enabled_categories(cfg)
    if not cats:
        return []

    missing: list[tuple[str, str]] = []
    for day in _day_list(first, last):
        for cat in cats:
            if store.report_exists(day, cat):
                continue
            if not store.papers_for(cat, tz_name, day,
                                    policy=cfg.get("crosslist_policy", "primary")):
                continue                      # 当天确实没有论文，不算遗漏
            missing.append((day, cat))
    return missing


def backfill_missing(
    cfg: Config,
    store: Store,
    *,
    use_llm: bool = True,
    progress: Progress | None = None,
    limit: int | None = None,
) -> list[Path]:
    """补齐缺失的日报。单次最多补 `backfill_max_per_run` 天，避免一次性刷爆模型额度。"""
    say: Progress = progress or (lambda m: log.info("%s", m))
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")
    cap = int(cfg["schedule"].get("backfill_max_per_run", 3)
              if limit is None else limit)
    pending = missing_days(cfg, store)
    if not pending:
        say("补缺检查：当月没有缺失的日报")
        return []

    days = sorted({d for d, _c in pending})
    initial = not store.kv_get("initial_backfill_done", "")
    if initial:
        cap = len(days)                    # 初次运行：当月一次补齐
    say(f"补缺检查：发现 {len(days)} 天缺日报（{days[0]} ~ {days[-1]}）")
    if len(days) > cap:
        say(f"  本次先补最早的 {cap} 天，其余下次继续")
        days = days[:cap]
    elif initial:
        say(f"  初次运行：一次补齐这 {len(days)} 天")

    math = MathService(store, assets_dir=cfg.assets_dir,
                       backend=cfg["report"].get("math_backend", "auto"))
    produced: list[Path] = []
    for day in days:
        say(f"  补 {day}")
        for sub in cfg.get("subscriptions", []):
            cat = (sub.get("category") or "").strip()
            if not cat or not sub.get("enabled", True):
                continue
            if store.report_exists(day, cat):
                continue
            papers = store.papers_for(cat, tz_name, day,
                                      policy=cfg.get("crosslist_policy", "primary"))
            if not papers:
                continue
            path, _ = build_category_report(
                cfg, store, category=cat, day=day, papers=papers, math=math,
                use_llm=use_llm, out_dir=_out_dir(cfg, sub),
                report_name=sub.get("report_name", ""), progress=say,
            )
            store.record_report(day, cat, str(path), len(papers))
            produced.append(path)
        write_index(cfg, store, day)
    if produced:
        say(f"补缺完成：生成 {len(produced)} 份报告")
    if initial and len(days) == len({d for d, _c in pending}):
        store.kv_set("initial_backfill_done", "1")
    return produced


def run_daily(
    cfg: Config,
    store: Store,
    *,
    day: str | None = None,
    only: list[str] | None = None,
    use_llm: bool = True,
    skip_collect: bool = False,
    progress: Progress | None = None,
) -> list[Path]:
    say: Progress = progress or (lambda m: log.info("%s", m))
    tz_name = cfg["schedule"].get("timezone", "Asia/Shanghai")
    day_arg = day                      # 记住调用方是否显式指定了某一天
    day = day or target_day(cfg)
    if not store.kv_get("first_run_day", ""):
        store.kv_set("first_run_day", day)
    run_id = store.start_run(f"daily {day}")
    produced: list[Path] = []
    try:
        if not skip_collect:
            collect(cfg, store, day=day, only=only, progress=say,
                    lookback_days=_auto_lookback(cfg, store, day))

        cats = enabled_categories(cfg)
        if only:
            cats = [c for c in cats if c in only] or list(only)

        math = MathService(store, assets_dir=cfg.assets_dir,
                           backend=cfg["report"].get("math_backend", "auto"))

        for sub in cfg.get("subscriptions", []):
            cat = (sub.get("category") or "").strip()
            if not cat or not sub.get("enabled", True) or cat not in cats:
                continue
            papers = store.papers_for(cat, tz_name, day,
                                      policy=cfg.get("crosslist_policy", "primary"))
            say(f"分类 {cat}：{len(papers)} 篇")
            if not papers:
                say(f"  {cat} 当日无新论文，跳过")
                continue
            path, _ = build_category_report(
                cfg, store, category=cat, day=day, papers=papers, math=math,
                use_llm=use_llm, out_dir=_out_dir(cfg, sub),
                report_name=sub.get("report_name", ""), progress=say,
            )
            rev = store.record_report(day, cat, str(path), len(papers))
            say(f"  已记录（第 {rev} 版）")
            produced.append(path)

        if cfg.get("output_mode") in ("combined", "both"):
            allp = store.all_papers_on(tz_name, day)
            allp = [p for p in allp if any(c in cats for c in store.categories_of(p.uid))]
            if allp:
                combined = (cfg.get("combined_out_dir") or "").strip()
                out = cfg.resolve(combined) if combined else cfg.reports_dir / "combined"
                path, _ = build_category_report(
                    cfg, store, category="combined", day=day, papers=allp, math=math,
                    use_llm=use_llm, out_dir=out, report_name="arXiv（合并）", progress=say,
                )
                store.record_report(day, "combined", str(path), len(allp))
                produced.append(path)

        # 默认日跑完后，把最近缺失的日期补上（显式指定 --day 时不补）
        if day_arg is None:
            try:
                backfill_missing(cfg, store, use_llm=use_llm, progress=say)
            except Exception as exc:  # noqa: BLE001 - 补缺失败不该让本轮整体失败
                log.warning("补缺失败（不影响本轮日报）：%s", exc)

        store.kv_set("last_success_day", day)
        idx = write_index(cfg, store, day)
        say(f"索引已更新：{idx}")
        log.debug("公式缓存 %s，数学服务统计 %s", store.math_count(), math.report_stats())
        store.finish_run(run_id, "ok", f"{len(produced)} 份报告")
    except Exception as exc:  # noqa: BLE001
        store.finish_run(run_id, "error", str(exc)[:500])
        raise
    return produced
