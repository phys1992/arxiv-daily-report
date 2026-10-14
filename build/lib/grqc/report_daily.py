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
from .render import ReportContext, load_theme_taxonomy, render_daily_html, resolve_fonts
from .store import Store
from .summarize import apply_summary, summarize_day

log = logging.getLogger(__name__)
Progress = Callable[[str], None]


def load_taxonomy_raw(path: Path) -> list[dict]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")).get("themes", [])
    except Exception:  # noqa: BLE001
        return [{"code": "other", "zh": "其他", "en": "Other"}]



def _out_dir(cfg: Config, sub: dict) -> Path:
    base = sub.get("out_dir") or f"reports/{sub['category']}"
    return cfg.resolve(base)


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

    taxonomy = load_taxonomy_raw(cfg.data_dir / "theme_taxonomy.json")
    labels, order = load_theme_taxonomy(cfg.data_dir / "theme_taxonomy.json")

    highlights, theme_notes, model = "", {}, ""
    bilingual = cfg.get("report_language", "zh") == "zh-en"
    if use_llm and papers:
        client = LLMClient(cfg, store)
        if client.ready:
            say(
                f"  调用模型 {client.describe()} 生成摘要（{len(papers)} 篇"
                + ("，中英对照需两遍）…" if bilingual else "）…")
            )
            if bilingual:
                from .summarize import summarize_day_bilingual

                summary = summarize_day_bilingual(
                    client, papers, category=category, day=day, taxonomy=taxonomy
                )
            else:
                summary = summarize_day(
                    client, papers, category=category, day=day, taxonomy=taxonomy, lang=lang
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

    # 豆腐块自检：报告里出现的字符是否都被随仓库分发的字体覆盖
    from .render import coverage_check

    missing = coverage_check(
        cfg.assets_dir,
        [p.title for p in papers] + [p.author_text() for p in papers] + [p.summary for p in papers],
    )
    if missing:
        say(
            "  警告：以下字符不在随仓库分发的字体中，将回退到系统字体（若系统也没有会显示为方块）："
            + " ".join(f"{c}(U+{ord(c):04X})" for c in missing[:20])
        )

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
            model=model,
            count=len(papers),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("存档日报摘要失败（不影响出报告）：%s", exc)

    reg, bold = resolve_fonts(cfg)
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
        font_regular=reg,
        font_bold=bold,
        bilingual=(cfg.get("report_language", "zh") == "zh-en"),
        notice=str((extra or {}).get("notice", "")),
    )
    html_text = render_daily_html(ctx, math)

    out_dir.mkdir(parents=True, exist_ok=True)
    if cfg["report"].get("keep_html"):
        (out_dir / f"{day}.html").write_text(html_text, encoding="utf-8")
    pdf_path = out_dir / f"{day}.pdf"
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
    day = day or target_day(cfg)
    run_id = store.start_run(f"daily {day}")
    produced: list[Path] = []
    try:
        if not skip_collect:
            collect(cfg, store, day=day, only=only, progress=say)

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
                out = cfg.resolve(cfg.get("combined_out_dir") or "reports/combined")
                path, _ = build_category_report(
                    cfg, store, category="combined", day=day, papers=allp, math=math,
                    use_llm=use_llm, out_dir=out, report_name="arXiv（合并）", progress=say,
                )
                store.record_report(day, "combined", str(path), len(allp))
                produced.append(path)

        idx = write_index(cfg, store, day)
        say(f"索引已更新：{idx}")
        log.debug("公式缓存 %s，数学服务统计 %s", store.math_count(), math.report_stats())
        store.finish_run(run_id, "ok", f"{len(produced)} 份报告")
    except Exception as exc:  # noqa: BLE001
        store.finish_run(run_id, "error", str(exc)[:500])
        raise
    return produced
