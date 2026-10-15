"""核心逻辑测试：数据模型、分类归属、去重、公式转换、配置与渲染。

全部使用标准库 unittest，不依赖网络：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from grqc.collect import assign, enabled_categories, target_day  # noqa: E402
from grqc.config import Config, DEFAULT_CONFIG  # noqa: E402
from grqc.mathrender import MathService, unicode_math  # noqa: E402
from grqc.models import Paper, parse_arxiv_id  # noqa: E402
from grqc.render import font_css, font_preflight, font_stack  # noqa: E402
from grqc.store import Store  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent

# 会被 Config 读取的环境变量：测试期间必须清空，否则结果取决于开发者的机器
_ENV_KEYS = (
    "GRQC_HOME", "GRQC_WEB_PASSWORD", "GRQC_WEB_HOST", "GRQC_WEB_PORT",
    "GRQC_PROVIDER", "GRQC_MODEL", "GRQC_BASE_URL",
    "GRQC_REPORT_LANGUAGE", "GRQC_UI_LANGUAGE",
    "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY", "CUSTOM_API_KEY",
)


class HermeticTestCase(unittest.TestCase):
    """在干净的环境变量下运行，保证测试结果与开发者本机环境无关。"""

    def setUp(self):
        self._saved_env = {k: os.environ.pop(k) for k in _ENV_KEYS if k in os.environ}

    def tearDown(self):
        for k, v in self._saved_env.items():
            os.environ[k] = v


def make_paper(aid="2610.12345", ver=1, primary="gr-qc", cats=None, **kw) -> Paper:
    return Paper(
        arxiv_id=aid, version=ver, title=kw.pop("title", "A Test Paper"),
        authors=kw.pop("authors", ["Alice", "Bob", "Carol", "Dave"]),
        abstract="abstract", published="2026-10-13T02:00:00Z",
        updated="2026-10-13T02:00:00Z", primary_category=primary,
        categories=cats if cats is not None else [primary], **kw,
    )


class ModelsTest(unittest.TestCase):
    def test_parse_arxiv_id(self):
        self.assertEqual(parse_arxiv_id("http://arxiv.org/abs/2610.18521v2"), ("2610.18521", 2))
        self.assertEqual(parse_arxiv_id("2610.18521v1"), ("2610.18521", 1))
        self.assertEqual(parse_arxiv_id("2610.18521"), ("2610.18521", 1))

    def test_uid_and_flags(self):
        self.assertEqual(make_paper().uid, "2610.12345v1")
        self.assertTrue(make_paper(ver=1).is_new)
        self.assertFalse(make_paper(ver=2).is_new)

    def test_author_limit_and_citation(self):
        p = make_paper()
        self.assertEqual(p.author_text(3), "Alice, Bob, Carol, et al.")
        self.assertEqual(make_paper(authors=["A", "B"]).author_text(3), "A, B")
        cite = p.citation(7)
        self.assertTrue(cite.startswith("[7] Alice, Bob, Carol, et al."))
        self.assertIn("arXiv:2610.12345", cite)
        self.assertIn("https://arxiv.org/abs/2610.12345", cite)

    def test_beijing_day_boundary(self):
        # UTC 16:00 之后已跨到北京次日
        p = make_paper()
        p.updated = "2026-10-12T16:00:00Z"
        self.assertEqual(p.day_str("Asia/Shanghai"), "2026-10-13")
        p.updated = "2026-10-12T15:59:00Z"
        self.assertEqual(p.day_str("Asia/Shanghai"), "2026-10-12")

    def test_roundtrip(self):
        p = make_paper(summary="要点", themes=["blackhole"])
        self.assertEqual(Paper.from_dict(p.to_dict()).summary, "要点")


class AssignTest(unittest.TestCase):
    def test_primary_policy_assigns_to_own_category(self):
        p = make_paper(primary="gr-qc", cats=["gr-qc", "hep-th"])
        a = assign(p, ["gr-qc", "hep-th"], "primary")
        self.assertEqual(a, {"gr-qc": True, "hep-th": False})

    def test_primary_policy_falls_back_when_primary_not_subscribed(self):
        p = make_paper(primary="hep-ph", cats=["hep-ph", "gr-qc"])
        a = assign(p, ["gr-qc", "hep-th"], "primary")
        self.assertEqual(a, {"gr-qc": True})

    def test_all_policy_keeps_every_match(self):
        p = make_paper(primary="gr-qc", cats=["gr-qc", "hep-th"])
        a = assign(p, ["gr-qc", "hep-th"], "all")
        self.assertEqual(a, {"gr-qc": True, "hep-th": True})

    def test_unmatched_category_ignored(self):
        p = make_paper(primary="astro-ph.CO", cats=["astro-ph.CO"])
        self.assertEqual(assign(p, ["gr-qc"], "primary"), {})


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "s.sqlite3")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_duplicate_detection(self):
        p = make_paper()
        self.assertTrue(self.store.add_paper(p, {"gr-qc": True}))
        self.assertFalse(self.store.add_paper(p, {"gr-qc": True}))   # 同 id+version 不重复入库

    def test_policy_filtering(self):
        a = make_paper(aid="2610.00001", primary="gr-qc", cats=["gr-qc", "hep-th"])
        b = make_paper(aid="2610.00002", primary="hep-th", cats=["gr-qc", "hep-th"])
        self.store.add_paper(a, {"gr-qc": True, "hep-th": False})
        self.store.add_paper(b, {"gr-qc": False, "hep-th": True})
        self.assertEqual(len(self.store.papers_for("gr-qc", "Asia/Shanghai", "2026-10-13")), 1)
        self.assertEqual(len(self.store.papers_for("gr-qc", "Asia/Shanghai", "2026-10-13", policy="all")), 2)

    def test_summary_persisted_and_reloaded(self):
        p = make_paper()
        self.store.add_paper(p, {"gr-qc": True})
        p.summary = "中文要点"
        p.themes = ["blackhole"]
        self.store.save_paper_summary(p)
        got = self.store.papers_for("gr-qc", "Asia/Shanghai", "2026-10-13")[0]
        self.assertEqual(got.summary, "中文要点")
        self.assertEqual(got.themes, ["blackhole"])

    def test_math_cache(self):
        self.assertIsNone(self.store.math_get("h"))
        self.store.math_put("h", "x^2", "<svg/>")
        self.assertEqual(self.store.math_get("h"), "<svg/>")
        self.assertEqual(self.store.math_count(), 1)

    def test_report_revision_increments(self):
        self.assertEqual(self.store.record_report("2026-10-13", "gr-qc", "/a.pdf", 3), 1)
        self.assertEqual(self.store.record_report("2026-10-13", "gr-qc", "/a.pdf", 4), 2)


class MathTest(unittest.TestCase):
    def test_extract(self):
        self.assertEqual(MathService.extract("关于 $F(R)$ 与 $\\alpha$ 的研究"), ["F(R)", "\\alpha"])
        self.assertEqual(MathService.extract("没有公式"), [])

    def test_unicode_conversion(self):
        cases = {
            "F(R)": "F(R)",
            "\\alpha": "α",
            "\\phi^4": "φ⁴",
            "3<q\\leq 10": "3<q≤ 10",
            "\\Lambda\\text{CDM}": "ΛCDM",
            "\\mathcal{N}=2": "𝒩=2",
        }
        for tex, want in cases.items():
            self.assertEqual(unicode_math(tex), want, tex)

    def test_frac_becomes_inline_division(self):
        self.assertEqual(unicode_math("\\frac{1}{2}"), "(1)/(2)")

    def test_to_html_falls_back_when_no_svg(self):
        tmp = tempfile.TemporaryDirectory()
        store = Store(Path(tmp.name) / "s.sqlite3")
        try:
            svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
            self.assertFalse(svc.mathjax_available)
            out = svc.to_html("关于 $\\alpha$ 的研究")
            self.assertIn("α", out)
            self.assertIn("math-unicode", out)
        finally:
            store.close()
            tmp.cleanup()


class ConfigTest(HermeticTestCase):
    def setUp(self):
        super().setUp()                      # 先清空环境变量，否则会覆盖被测配置
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.json"
        self.cfg = Config(path=self.path)

    def tearDown(self):
        self.tmp.cleanup()
        super().tearDown()

    def test_defaults(self):
        self.assertEqual(self.cfg["subscriptions"][0]["category"], "gr-qc")
        self.assertEqual(self.cfg["schedule"]["timezone"], "Asia/Shanghai")
        self.assertEqual(self.cfg["report"]["font_family"], "")   # 空 = 内置默认字体栈
        self.assertTrue(self.cfg["llm"]["disable_thinking"])

    def test_save_reload_and_patch(self):
        self.cfg.set_api_key("deepseek", "sk-abcdefghijklmnopqrstuvwxyz0123456789")
        self.cfg.update({"subscriptions": [{"category": "hep-th", "enabled": True}]})
        self.cfg.save()
        again = Config(path=self.path)
        self.assertEqual(again["subscriptions"][0]["category"], "hep-th")
        self.assertTrue(again.api_key("deepseek").startswith("sk-"))

    def test_environment_is_clean(self):
        for k in ("DEEPSEEK_API_KEY", "GRQC_WEB_PASSWORD", "GRQC_HOME"):
            self.assertNotIn(k, os.environ, "测试环境残留 " + k)

    def test_key_masking_never_leaks_plaintext(self):
        key = "sk-2431aaaaaaaabbbbccccddddeeeeffff"
        self.cfg.set_api_key("deepseek", key)
        masked = self.cfg.mask_key("deepseek")
        self.assertNotIn("aaaaaaaabbbb", masked)
        self.assertTrue(masked.startswith("sk-243"))
        self.assertTrue(masked.endswith("ffff"))

    def test_secrets_file_permissions(self):
        self.cfg.set_api_key("openai", "sk-test")
        self.cfg.save()
        mode = (self.path.with_name("secrets.json").stat().st_mode) & 0o777
        self.assertEqual(mode, 0o600)

    def test_home_dir_source_checkout_vs_installed(self):
        """源码检出用项目根；pip 安装（site-packages）用用户数据目录。"""
        import os as _os
        from unittest import mock

        from grqc import config as _cfg

        # 源码检出：项目根
        with mock.patch.object(_cfg, "_is_source_checkout", return_value=True):
            self.assertEqual(_cfg.home_dir(), _cfg.PROJECT_ROOT)
        # 已安装：落到 XDG 数据目录，而不是 site-packages
        with mock.patch.object(_cfg, "_is_source_checkout", return_value=False):
            home = _cfg.home_dir()
            self.assertNotEqual(home, _cfg.PROJECT_ROOT)
            self.assertEqual(home.name, _cfg.APP_NAME)
            self.assertNotIn("site-packages", str(home))
        # GRQC_HOME 优先级最高
        _os.environ["GRQC_HOME"] = "/tmp/grqc-home-test"
        try:
            self.assertEqual(str(_cfg.home_dir()), "/tmp/grqc-home-test")
        finally:
            _os.environ.pop("GRQC_HOME", None)

    def test_is_source_checkout_true_in_repo(self):
        from grqc.config import _is_source_checkout

        self.assertTrue(_is_source_checkout(), "本仓库应被判定为源码检出")

    def test_data_asset_falls_back_to_project(self):
        """GRQC_HOME 指到项目外时，随仓库的分类表/主题表仍要能找到。"""
        import tempfile as _tf

        with _tf.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "config.json",
                         overrides={"paths": {"data": td}})
            self.assertEqual(cfg.data_dir, Path(td))
            cats = cfg.data_asset("arxiv_categories.json")
            themes = cfg.data_asset("theme_taxonomy.json")
            self.assertTrue(cats.exists(), "分类表应回退到项目 data/")
            self.assertTrue(themes.exists(), "主题表应回退到项目 data/")
            self.assertEqual(cats.parent, PROJECT / "data")
            # 自己目录里有的优先
            own = Path(td) / "theme_taxonomy.json"
            own.write_text('{"themes": []}', encoding="utf-8")
            self.assertEqual(cfg.data_asset("theme_taxonomy.json"), own)

    def test_assets_dir_honours_env_override(self):
        import os as _os

        from grqc.config import Config as _C

        _os.environ["GRQC_ASSETS"] = "/tmp/grqc-assets-test"
        try:
            self.assertEqual(str(_C(path=Path("/tmp/x.json")).assets_dir), "/tmp/grqc-assets-test")
        finally:
            _os.environ.pop("GRQC_ASSETS", None)

    def test_relative_paths_resolve_against_home(self):
        p = self.cfg.resolve("reports")
        self.assertTrue(p.is_absolute())
        self.assertEqual(p.name, "reports")
        self.assertTrue(self.cfg.resolve("/tmp/x").is_absolute())

    def test_everything_in_sync_with_example(self):
        example = json.loads((PROJECT / "config.example.json").read_text(encoding="utf-8"))
        for key in example:
            self.assertIn(key, DEFAULT_CONFIG, f"config.example.json 出现未知键 {key}")


class RenderTest(unittest.TestCase):
    def test_font_stack_is_latin_first_then_cjk(self):
        """字体栈顺序决定「英文用西文字体、汉字用中文字体」，不能反过来。"""
        stack = font_stack()
        self.assertIn("sans-serif", stack)
        # 西文族在前
        self.assertLess(stack.index("DejaVu Sans"), stack.index("Source Han Sans SC"))
        # 中文字体族都在，且思源黑体优先
        for fam in ("Source Han Sans SC", "Noto Sans CJK SC", "WenQuanYi Micro Hei",
                    "Microsoft YaHei"):
            self.assertIn(fam, stack)
        self.assertLess(stack.index("Source Han Sans SC"), stack.index("Microsoft YaHei"))

    def test_font_stack_honours_config_override(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json",
                         overrides={"report": {"font_family": '"My Font", serif'}})
            self.assertEqual(font_stack(cfg), '"My Font", serif')
            # 留空则回到默认栈
            cfg.update({"report": {"font_family": ""}})
            self.assertIn("Source Han Sans SC", font_stack(cfg))

    def test_font_css_has_no_bundled_fonts(self):
        """默认不再输出任何 @font-face：字体由系统提供。"""
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            css = font_css(cfg)
            self.assertNotIn("@font-face", css)
            self.assertIn("html{font-family:", css)
            self.assertIn("body{font-family:", css)
            self.assertIn("Source Han Sans SC", css)
            # 自定义字体文件才会产生 @font-face
            cfg.update({"report": {"font_file": str(PROJECT / "README.md")}})
            self.assertIn("@font-face", font_css(cfg))

    def test_no_bundled_font_files_in_repo(self):
        self.assertFalse((PROJECT / "assets/fonts").exists(),
                         "字体不应再随仓库分发")

    def test_page_margin_boxes_declare_font_stack(self):
        """回归：页眉页脚不继承 body 的字体，必须显式声明。"""
        from grqc.render import base_css

        css = base_css("gr-qc · 2026-10-13", "")
        for box in ("@bottom-center", "@top-right"):
            self.assertIn(box, css)
            segment = css.split(box, 1)[1].split("}", 1)[0]
            self.assertIn("font-family", segment, f"{box} 缺少 font-family")
        self.assertIn("Source Han Sans SC", css)

    def test_font_preflight_reports_missing_cjk(self):
        """没有中文字体时要给出可操作的告警。"""
        from unittest import mock

        from grqc import render as _r

        fake = mock.Mock(return_value=mock.Mock(stdout="", returncode=0))
        with mock.patch.object(_r, "font_stack", return_value="sans-serif"):
            with mock.patch("shutil.which", return_value="/usr/bin/fc-list"):
                with mock.patch("subprocess.run", fake):
                    warns = font_preflight()
        self.assertTrue(warns, "应给出缺少中文字体的告警")
        self.assertIn("中文字体", warns[0])

    def test_font_preflight_silent_when_fonts_present(self):
        from unittest import mock

        fake = mock.Mock(return_value=mock.Mock(
            stdout="思源黑体\nSource Han Sans SC\nNoto Sans CJK SC\n", returncode=0))
        with mock.patch("shutil.which", return_value="/usr/bin/fc-list"):
            with mock.patch("subprocess.run", fake):
                self.assertEqual(font_preflight(), [])

    def test_category_and_theme_assets_are_complete(self):
        cats = json.loads((PROJECT / "data/arxiv_categories.json").read_text(encoding="utf-8"))
        self.assertEqual(len(cats), 155)
        self.assertEqual(cats["gr-qc"], "General Relativity and Quantum Cosmology")
        self.assertIn("hep-th", cats)
        themes = json.loads((PROJECT / "data/theme_taxonomy.json").read_text(encoding="utf-8"))["themes"]
        codes = {t["code"] for t in themes}
        self.assertIn("other", codes)
        self.assertIn("blackhole", codes)
        for t in themes:
            self.assertTrue(t.get("zh") and t.get("en"), t)


class ScheduleHelpersTest(HermeticTestCase):
    def test_target_day_uses_offset(self):
        cfg = Config(path=Path("/tmp/grqc-sched-test.json"))
        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(target_day(cfg, now), "2026-10-13")

    def test_enabled_categories_filters_and_dedupes(self):
        cfg = Config(path=Path("/tmp/grqc-sched-test.json"), overrides={
            "subscriptions": [
                {"category": "gr-qc", "enabled": True},
                {"category": "hep-th", "enabled": False},
                {"category": "gr-qc", "enabled": True},
                {"category": "", "enabled": True},
            ]
        })
        self.assertEqual(enabled_categories(cfg), ["gr-qc"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class BilingualTest(unittest.TestCase):
    """中英对照：合并逻辑与章节标题（不消耗模型额度）。"""

    def test_join_paragraphs(self):
        from grqc.summarize import _join_paragraphs

        self.assertEqual(_join_paragraphs("中文", "English"), "中文\nEnglish")
        self.assertEqual(_join_paragraphs("中文", ""), "中文")
        self.assertEqual(_join_paragraphs("", "English"), "English")
        self.assertEqual(_join_paragraphs("", ""), "")

    def test_merge_bilingual_summaries(self):
        from grqc.summarize import DaySummary, _join_paragraphs

        zh = DaySummary(model="m", ok=True, highlights="中文要点",
                        theme_notes={"blackhole": "黑洞综述"},
                        papers={"a": {"summary": "中文摘要", "themes": ["blackhole"]}})
        en = DaySummary(model="m", ok=True, highlights="English highlights",
                        theme_notes={"blackhole": "Black hole overview"},
                        papers={"a": {"summary": "English abstract", "themes": ["blackhole"]}})
        merged = DaySummary(model="m", ok=True)
        merged.highlights = _join_paragraphs(zh.highlights, en.highlights)
        merged.theme_notes["blackhole"] = _join_paragraphs(
            zh.theme_notes["blackhole"], en.theme_notes["blackhole"])
        merged.papers["a"] = {"summary": _join_paragraphs("中文摘要", "English abstract"),
                              "themes": ["blackhole"]}
        self.assertIn("中文要点", merged.highlights)
        self.assertIn("English highlights", merged.highlights)
        self.assertIn("黑洞综述", merged.theme_notes["blackhole"])
        self.assertIn("English abstract", merged.papers["a"]["summary"])

    def test_heading_switches_with_bilingual_flag(self):
        from grqc.render import heading

        zh_only = heading("zh", "report.papers", False)
        self.assertEqual(zh_only, "论文清单")
        both = heading("zh", "report.papers", True)
        self.assertIn("论文清单", both)
        self.assertIn("Papers", both)
        self.assertIn("en-label", both)

    def test_english_report_labels(self):
        from grqc.render import heading

        self.assertEqual(heading("en", "report.papers", False), "Papers")
        self.assertEqual(heading("en", "report.highlights", False), "Highlights")

    def test_bilingual_daily_html_renders(self):
        """不调用模型，只验证双语版式能渲染出 HTML。"""
        import tempfile as _tf

        from grqc.render import ReportContext, render_daily_html

        tmp = _tf.TemporaryDirectory()
        store = Store(Path(tmp.name) / "s.sqlite3")
        try:
            svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
            cfg = Config(path=Path(tmp.name) / "c.json")
            ctx = ReportContext(
                category="gr-qc", category_name="General Relativity and Quantum Cosmology",
                day="2026-10-13", papers=[make_paper(summary="中文要点\nEnglish note",
                                                    themes=["blackhole"])],
                assets_dir=PROJECT / "assets", bilingual=True,
                font_css_text=font_css(cfg),
                theme_labels={"blackhole": ("黑洞物理", "Black Holes"), "other": ("其他", "Other")},
                theme_order=["blackhole", "other"],
            )
            out = render_daily_html(ctx, svc)
            self.assertIn("论文清单", out)
            self.assertIn("Papers", out)
            self.assertIn("中文要点", out)
            self.assertIn("English note", out)
            # 日报不再单列参考文献（论文清单已含作者/标题/arXiv 号/链接）
            self.assertNotIn("参考文献", out)
            self.assertNotIn("report.references", out)
        finally:
            store.close()
            tmp.cleanup()


class MonthlyTest(HermeticTestCase):
    """月报：目标月份、日期引用、依据日报浓缩。"""

    def _cfg(self, tmp: str) -> Config:
        return Config(path=Path(tmp) / "config.json")

    def test_target_month_is_previous_month(self):
        from grqc.report_monthly import target_month

        z = dt.timezone(dt.timedelta(hours=8))
        self.assertEqual(target_month(dt.datetime(2026, 11, 1, 19, 0, tzinfo=z)), "2026-10")
        self.assertEqual(target_month(dt.datetime(2026, 10, 14, 12, 0, tzinfo=z)), "2026-09")
        self.assertEqual(target_month(dt.datetime(2026, 1, 1, 12, 0, tzinfo=z)), "2025-12")

    def test_should_run_monthly_only_on_first_day(self):
        from grqc.report_monthly import should_run_monthly

        z = dt.timezone(dt.timedelta(hours=8))
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            self.assertEqual(should_run_monthly(cfg, dt.datetime(2026, 11, 1, 19, 0, tzinfo=z)), "2026-10")
            self.assertIsNone(should_run_monthly(cfg, dt.datetime(2026, 11, 2, 19, 0, tzinfo=z)))
            cfg.update({"schedule": {"auto_monthly": False}})
            self.assertIsNone(should_run_monthly(cfg, dt.datetime(2026, 12, 1, 19, 0, tzinfo=z)))

    def test_day_label_and_validation(self):
        from grqc.report_monthly import _valid_days, day_label

        self.assertEqual(day_label("2026-10-07"), "10月7日")
        self.assertEqual(day_label("2026-10-07", "en"), "Oct 7")
        self.assertEqual(
            _valid_days(["10-07", "2026-10-13", "10-32", "09-01", ""], "2026-10"),
            ["2026-10-07", "2026-10-13"],
        )

    def test_no_artificial_length_limits(self):
        """月报不设人为字数上限：准确优先，简明其次。"""
        from grqc import report_monthly as rm

        self.assertFalse(hasattr(rm, "TOP_THEMES"), "不应再有主题条数上限")
        self.assertFalse(hasattr(rm, "_clip"), "不应再对主题要点做截断")
        src = Path(rm.__file__).read_text(encoding="utf-8")
        for banned in ("不超过", "最多 2 句", "250–400", "Brevity is the first"):
            self.assertNotIn(banned, src, f"配额要求里不应出现 {banned!r}")
        # 准确性要求必须写明
        self.assertIn("力求准确是第一要求", src)
        self.assertIn("Accuracy comes first", src)

    def test_all_substantive_themes_are_kept(self):
        """有论文的主题都应保留，不做 Top-N 裁剪。"""
        import json as _json
        import tempfile as _tf

        from grqc.report_monthly import collect_month, monthly_llm, MonthlyData

        class FakeClient:
            ready = True

            def describe(self):
                return "fake"

            def chat_json(self, system, user, *, schema_hint="", max_tokens=None, retries=3):
                if "themes" in schema_hint:
                    return {"themes": [
                        {"code": f"t{i}", "text": f"主题 {i} 的实质进展。", "days": ["10-07"], "count": 1}
                        for i in range(14)
                    ]}
                return {"overview": "概览。", "highlights": [{"text": "进展", "days": ["10-07"]}]}

        with _tf.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            labels = {f"t{i}": (f"主题{i}", f"Theme{i}") for i in range(14)}
            labels["other"] = ("其他", "Other")
            order = list(labels)
            data = MonthlyData(
                month="2026-10", days=["2026-10-07"],
                digests={"2026-10-07": {"highlights": "x", "count": 14,
                                        "themes": {f"t{i}": f"某日主题{i}综述。" for i in range(14)}}},
                theme_counts={f"t{i}": 1 for i in range(14)},
            )
            monthly_llm(FakeClient(), data, lang="zh", labels=labels, order=order,
                        progress=lambda m: None)
            self.assertEqual(len(data.theme_notes), 14, "14 个有论文的主题都应保留")

    def test_collect_month_merges_multiple_categories(self):
        from grqc.report_monthly import collect_month

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "s.sqlite3")
            try:
                store.save_day_digest("2026-10-07", "gr-qc", highlights="A 要点",
                                      themes={"blackhole": "黑洞甲"}, count=5)
                store.save_day_digest("2026-10-07", "hep-th", highlights="B 要点",
                                      themes={"blackhole": "黑洞乙", "holography": "全息丙"}, count=3)
                store.save_day_digest("2026-10-13", "gr-qc", highlights="C 要点",
                                      themes={"cosmology": "宇宙丁"}, count=7)
                # 必须同时订阅两个分类，否则未被订阅的分类会被过滤掉（这是预期行为）
                cfg = self._cfg(td)
                cfg.update({"subscriptions": [
                    {"category": "gr-qc", "enabled": True},
                    {"category": "hep-th", "enabled": True},
                ]})
                data = collect_month(cfg, store, "2026-10")
                self.assertEqual(data.days, ["2026-10-07", "2026-10-13"])
                merged = data.digests["2026-10-07"]
                self.assertIn("A 要点", merged["highlights"])
                self.assertIn("B 要点", merged["highlights"])
                self.assertIn("黑洞甲", merged["themes"]["blackhole"])
                self.assertIn("黑洞乙", merged["themes"]["blackhole"])
                self.assertEqual(merged["count"], 8)
                self.assertIn("holography", data.theme_counts)
            finally:
                store.close()

    def test_monthly_html_has_dates_and_no_reference_list(self):
        from grqc.report_monthly import MonthlyData, render_monthly_html

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            data = MonthlyData(
                month="2026-10",
                days=["2026-10-07", "2026-10-13"],
                digests={},
                paper_count=61,
                overview="本月主线是黑洞响应与引力波观测。",
                highlights=[{"text": "证明 Kerr-Newman 静态 Love 数整体为零", "days": ["2026-10-13"]}],
                theme_notes=[{"code": "blackhole", "text": "黑洞方向进展集中在响应函数。",
                              "days": ["2026-10-07", "2026-10-13"], "count": 6}],
                model="mock",
            )
            out = render_monthly_html(cfg, data, scope_name="gr-qc")
            self.assertIn("本月概览", out)
            self.assertIn("主要进展", out)
            self.assertIn("分主题要点", out)
            self.assertIn("10月13日", out)          # 日期标注
            self.assertIn("10月7日", out)
            # 不列文献表、不列论文清单、不出现 arXiv 号
            self.assertNotIn("参考文献", out)
            self.assertNotIn("当月论文清单", out)
            self.assertNotIn("arXiv:", out)

    def test_daily_html_drops_reference_section(self):
        from grqc.render import ReportContext, render_daily_html

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "s.sqlite3")
            try:
                svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
                cfg = self._cfg(td)
                ctx = ReportContext(
                    category="gr-qc", category_name="gr-qc", day="2026-10-13",
                    papers=[make_paper()], assets_dir=PROJECT / "assets",
                    font_css_text=font_css(cfg),
                    theme_labels={"other": ("其他", "Other")}, theme_order=["other"],
                )
                out = render_daily_html(ctx, svc)
                self.assertIn("论文清单", out)
                self.assertIn("arXiv:2610.12345", out)   # 清单里仍带 arXiv 号与链接
                self.assertNotIn("参考文献", out)
            finally:
                store.close()
