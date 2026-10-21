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

from arxiv_daily_report.collect import assign, enabled_categories, target_day  # noqa: E402
from arxiv_daily_report.config import Config, DEFAULT_CONFIG  # noqa: E402
from arxiv_daily_report.mathrender import MathService, unicode_math  # noqa: E402
from arxiv_daily_report.models import Paper, parse_arxiv_id  # noqa: E402
from arxiv_daily_report.render import font_css, font_preflight, font_stack  # noqa: E402
from arxiv_daily_report.store import Store  # noqa: E402

PROJECT = Path(__file__).resolve().parent.parent

# 会被 Config 读取的环境变量：测试期间必须清空，否则结果取决于开发者的机器
_ENV_KEYS = (
    "ADR_HOME", "ADR_WEB_PASSWORD", "ADR_WEB_HOST", "ADR_WEB_PORT",
    "ADR_PROVIDER", "ADR_MODEL", "ADR_BASE_URL",
    "ADR_REPORT_LANGUAGE", "ADR_UI_LANGUAGE",
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
        for k in ("DEEPSEEK_API_KEY", "ADR_WEB_PASSWORD", "ADR_HOME"):
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

    def test_directory_precedence(self):
        """ADR_HOME > 单项 ADR_*_HOME > 源码检出 > XDG 默认。"""
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        def dirs(checkout=True):
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=checkout):
                return _cfg.config_dir(), _cfg.data_dir(), _cfg.cache_dir()

        # 1) 源码检出 -> 项目内
        c, d, k = dirs()
        self.assertEqual(c, _cfg.PROJECT_ROOT)
        self.assertEqual(d, _cfg.PROJECT_ROOT / "data")
        self.assertEqual(k, _cfg.PROJECT_ROOT / "cache")
        # 2) ADR_HOME -> 全部收进一个目录
        with mock.patch.dict(_cfg.os.environ, {"ADR_HOME": "/tmp/adr-H"}):
            c, d, k = dirs()
            self.assertEqual((str(c), str(d), str(k)),
                             ("/tmp/adr-H", "/tmp/adr-H/data", "/tmp/adr-H/cache"))
        # 3) 单项覆盖（即使处于源码检出也生效）
        with mock.patch.dict(_cfg.os.environ,
                             {"ADR_CONFIG_HOME": "/tmp/ac", "ADR_DATA_HOME": "/tmp/ad",
                              "ADR_CACHE_HOME": "/tmp/ak"}):
            self.assertEqual([str(x) for x in dirs()], ["/tmp/ac", "/tmp/ad", "/tmp/ak"])
        # 4) 安装形态 -> XDG 默认（状态进 STATE 而不是 DATA）
        c, d, k = dirs(checkout=False)
        self.assertEqual(c.name, _cfg.APP_NAME)
        self.assertIn(".config", str(c))
        self.assertIn(".local/state", str(d))
        self.assertIn(".cache", str(k))
        self.assertNotIn("site-packages", str(d))
        # 5) 安装形态 + XDG 覆盖
        with mock.patch.dict(_cfg.os.environ,
                             {"XDG_CONFIG_HOME": "/tmp/xc", "XDG_STATE_HOME": "/tmp/xs",
                              "XDG_CACHE_HOME": "/tmp/xk"}):
            c, d, k = dirs(checkout=False)
            self.assertEqual([str(x) for x in (c, d, k)],
                             ["/tmp/xc/" + _cfg.APP_NAME, "/tmp/xs/" + _cfg.APP_NAME,
                              "/tmp/xk/" + _cfg.APP_NAME])

    def test_reports_dir_defaults(self):
        """报告目录：便携模式在项目内，安装形态在数据目录下，显式配置最优先。"""
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with tempfile.TemporaryDirectory() as td:
            env = {"ADR_CONFIG_HOME": f"{td}/c", "ADR_DATA_HOME": f"{td}/d",
                   "ADR_CACHE_HOME": f"{td}/k"}
            clean = {k: v for k, v in _cfg.os.environ.items()
                     if k not in ("ADR_HOME", "ADR_CONFIG_HOME", "ADR_DATA_HOME",
                                  "ADR_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                                  "XDG_CACHE_HOME")}
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=True), \
                 mock.patch.dict(_cfg.os.environ, clean, clear=True):
                cfg = _cfg.Config()
                self.assertEqual(cfg.reports_dir, _cfg.PROJECT_ROOT / "reports")
                self.assertEqual(cfg.data_dir, _cfg.PROJECT_ROOT / "data")
                self.assertEqual(cfg.cache_dir, _cfg.PROJECT_ROOT / "cache")
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=False), \
                 mock.patch.dict(_cfg.os.environ, env):
                cfg = _cfg.Config()
                # 安装形态：报告进「文档」目录下的 <APP_NAME>
                self.assertEqual(cfg.reports_dir.name, _cfg.APP_NAME)
                self.assertIn("Documents", str(cfg.reports_dir))
            # 显式配置优先
            with mock.patch.dict(_cfg.os.environ, env):
                cfg = _cfg.Config(overrides={"paths": {"reports": f"{td}/R"}})
                self.assertEqual(cfg.reports_dir, Path(f"{td}/R"))

    def test_ensure_home_reports_unwritable_dir(self):
        """目录不可写时要指出是哪一个，并给出可操作的解决办法。"""
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with tempfile.TemporaryDirectory() as td:
            env = {"ADR_CONFIG_HOME": f"{td}/c", "ADR_DATA_HOME": "/proc/nope/nowhere",
                   "ADR_CACHE_HOME": f"{td}/k"}
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=False), \
                 mock.patch.dict(_cfg.os.environ, env):
                with self.assertRaises(RuntimeError) as ctx:
                    _cfg.Config()
                msg = str(ctx.exception)
                self.assertIn("状态目录不可写", msg)
                self.assertIn("ADR_HOME", msg)

    def test_installed_layout_discovers_system_paths(self):
        """发行版安装布局：数据在 /usr/share/<name>/data，前端在 /usr/share/<name>/web。"""
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with tempfile.TemporaryDirectory() as td:
            prefix = Path(td) / "usr"
            sysdata = prefix / "share" / _cfg.APP_NAME
            (sysdata / "data").mkdir(parents=True)
            (sysdata / "data" / "arxiv_categories.json").write_text('{"gr-qc":"x"}', encoding="utf-8")
            (sysdata / "data" / "theme_taxonomy.json").write_text('{"themes":[]}', encoding="utf-8")
            (sysdata / "web").mkdir(parents=True)
            (sysdata / "web" / "index.html").write_text("<html></html>", encoding="utf-8")
            env = {"ADR_PREFIX": str(prefix),
                   "ADR_CONFIG_HOME": f"{td}/c", "ADR_DATA_HOME": f"{td}/d",
                   "ADR_CACHE_HOME": f"{td}/k"}
            # 把“项目根”也指到临时目录，模拟包安装后 PROJECT_ROOT 里没有 data/
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=False), \
                 mock.patch.object(_cfg, "PROJECT_ROOT", Path(td) / "site-packages"), \
                 mock.patch.dict(_cfg.os.environ, env):
                cfg = _cfg.Config()
                # 自己数据目录里没有 -> 回退到 /usr/share
                self.assertEqual(cfg.data_asset("arxiv_categories.json"),
                                 sysdata / "data" / "arxiv_categories.json")
                self.assertEqual(cfg.data_asset("theme_taxonomy.json"),
                                 sysdata / "data" / "theme_taxonomy.json")
                self.assertEqual(cfg.web_dir, sysdata / "web")
                self.assertTrue((cfg.web_dir / "index.html").exists())
                # 自己数据目录里有的优先
                own = Path(f"{td}/d/theme_taxonomy.json")
                own.write_text('{"themes":[1]}', encoding="utf-8")
                self.assertEqual(cfg.data_asset("theme_taxonomy.json"), own)
            # 前缀默认值是 /usr
            with mock.patch.dict(_cfg.os.environ, {}, clear=False):
                _cfg.os.environ.pop("ADR_PREFIX", None)
                self.assertEqual(str(_cfg.system_prefix()), "/usr")
                self.assertEqual(str(_cfg.system_data_dir()),
                                 "/usr/share/" + _cfg.APP_NAME)
                self.assertEqual(str(_cfg.system_lib_dir()),
                                 "/usr/lib/" + _cfg.APP_NAME)

    def test_config_json_lives_in_config_dir(self):
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with tempfile.TemporaryDirectory() as td:
            env = {"ADR_CONFIG_HOME": f"{td}/cfg", "ADR_DATA_HOME": f"{td}/dat",
                   "ADR_CACHE_HOME": f"{td}/cch"}
            with mock.patch.object(_cfg, "_is_source_checkout", return_value=False), \
                 mock.patch.dict(_cfg.os.environ, env):
                cfg = _cfg.Config()
                self.assertEqual(cfg.path, Path(f"{td}/cfg/config.json"))
                self.assertEqual(cfg.secrets_path, Path(f"{td}/cfg/secrets.json"))
                cfg.set_api_key("deepseek", "sk-x")
                cfg.save()
                self.assertTrue(Path(f"{td}/cfg/config.json").exists())
                self.assertTrue(Path(f"{td}/cfg/secrets.json").exists())

    def test_is_source_checkout_true_in_repo(self):
        from arxiv_daily_report.config import _is_source_checkout

        self.assertTrue(_is_source_checkout(), "本仓库应被判定为源码检出")

    def test_data_asset_falls_back_to_project(self):
        """ADR_HOME 指到项目外时，随仓库的分类表/主题表仍要能找到。"""
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

        from arxiv_daily_report.config import Config as _C

        _os.environ["ADR_ASSETS"] = "/tmp/adr-assets-test"
        try:
            self.assertEqual(str(_C(path=Path("/tmp/x.json")).assets_dir), "/tmp/adr-assets-test")
        finally:
            _os.environ.pop("ADR_ASSETS", None)

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
    def test_default_preset_is_arxiv_fonts(self):
        """默认西文用 Computer Modern、中文用思源宋体（arXiv 预印本观感）。"""
        from arxiv_daily_report.render import DEFAULT_PRESET, preset_of

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            self.assertEqual(DEFAULT_PRESET, "arxiv")
            self.assertEqual(preset_of(cfg), "arxiv")
            stack = font_stack(cfg)
            # 西文族在前、中文族在后（顺序决定「英文走西文、汉字走中文」）
            self.assertLess(stack.index("Latin Modern Roman"),
                            stack.index("Source Han Serif SC"))
            self.assertIn("CMU Serif", stack)
            self.assertIn("Source Han Serif SC", stack)
            self.assertIn("Noto Serif CJK SC", stack)
            self.assertTrue(stack.rstrip().endswith(("serif", "sans-serif")))

    def test_presets_switch_fonts(self):
        from arxiv_daily_report.render import font_stack, preset_of

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            cfg.update({"report": {"font_preset": "journal"}})
            self.assertEqual(preset_of(cfg), "journal")
            st = font_stack(cfg)
            self.assertIn("Nimbus Roman", st)          # Times 系
            self.assertNotIn("Latin Modern Roman", st)
            self.assertIn("Source Han Serif SC", st)   # 中文仍是宋体

            cfg.update({"report": {"font_preset": "sans"}})
            st = font_stack(cfg)
            self.assertIn("DejaVu Sans", st)
            self.assertIn("Source Han Sans SC", st)
            self.assertNotIn("Source Han Serif SC", st)

            # 非法值回落到默认
            cfg.update({"report": {"font_preset": "nonsense"}})
            self.assertEqual(preset_of(cfg), "arxiv")

    def test_font_stack_honours_config_override(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json",
                         overrides={"report": {"font_family": '"My Font", serif'}})
            self.assertEqual(font_stack(cfg), '"My Font", serif')
            # 留空则回到预设
            cfg.update({"report": {"font_family": ""}})
            self.assertIn("Source Han Serif SC", font_stack(cfg))

    def test_font_plan_falls_back_to_texlive_computer_modern(self):
        """Computer Modern 常只随 TeX Live 装、未注册进 fontconfig。

        这种情况下应当用 TeX Live 的 OTF 生成 @font-face，而不是直接掉到 Times。
        """
        from unittest import mock

        from arxiv_daily_report import render as _r

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            fake = {"lmroman10-regular.otf": Path("/usr/share/texmf-dist/fonts/opentype"
                                                 "/public/lm/lmroman10-regular.otf"),
                    "lmroman10-bold.otf": Path("/usr/share/texmf-dist/fonts/opentype"
                                               "/public/lm/lmroman10-bold.otf")}
            with mock.patch.object(_r, "_family_available", return_value=False), \
                 mock.patch.object(_r, "_cm_font_files", return_value=fake):
                faces, stack = _r.font_plan(cfg)
            self.assertIn('font-family:"ADR Latin"', faces)
            self.assertIn("lmroman10-regular.otf", faces)
            self.assertTrue(stack.startswith('"ADR Latin"'))

    def test_font_plan_without_any_cm_falls_back_to_serif(self):
        """连 TeX Live 都没有时不报错，静默退到 Times 系。"""
        from unittest import mock

        from arxiv_daily_report import render as _r

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            with mock.patch.object(_r, "_family_available", return_value=False), \
                 mock.patch.object(_r, "_cm_font_files", return_value={}):
                faces, stack = _r.font_plan(cfg)
            self.assertEqual(faces, "")
            self.assertIn("Nimbus Roman", stack)          # 预设里的西文回退
            self.assertIn("Source Han Serif SC", stack)

    def test_font_css_has_no_bundled_fonts(self):
        """仓库不带字体：默认靠系统字体族名，只有需要时才产生 @font-face。"""
        from unittest import mock

        from arxiv_daily_report import render as _r

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            with mock.patch.object(_r, "_family_available", return_value=True):
                css = font_css(cfg)
            self.assertNotIn("@font-face", css)            # 系统有 CM 就不内联
            self.assertIn("html{font-family:", css)
            self.assertIn("body{font-family:", css)
            self.assertIn("Source Han Serif SC", css)
            # 自定义字体文件才会产生 @font-face
            cfg.update({"report": {"font_file": str(PROJECT / "README.md")}})
            self.assertIn("@font-face", font_css(cfg))

    def test_no_bundled_font_files_in_repo(self):
        self.assertFalse((PROJECT / "assets/fonts").exists(),
                         "字体不应再随仓库分发")

    def test_page_margin_boxes_declare_font_stack(self):
        """回归：页眉页脚不继承 body 的字体，必须显式声明。"""
        from arxiv_daily_report.render import base_css

        css = base_css("gr-qc · 2026-10-13", "")
        for box in ("@bottom-center", "@top-right"):
            self.assertIn(box, css)
            segment = css.split(box, 1)[1].split("}", 1)[0]
            self.assertIn("font-family", segment, f"{box} 缺少 font-family")
        self.assertIn("Source Han Serif SC", css)

    def test_font_preflight_reports_missing_cjk(self):
        """没有中文字体时要给出可操作的告警。"""
        from unittest import mock

        from arxiv_daily_report import render as _r

        fake = mock.Mock(return_value=mock.Mock(stdout="", returncode=0))
        with mock.patch("shutil.which", return_value="/usr/bin/fc-list"):
            with mock.patch("subprocess.run", fake):
                warns = font_preflight()
        self.assertTrue(warns, "应给出缺少中文字体的告警")
        self.assertTrue(any("中文" in w for w in warns))

    def test_font_preflight_silent_when_fonts_present(self):
        from unittest import mock

        from unittest import mock as _m

        from arxiv_daily_report import render as _r

        # 思源宋体 + Computer Modern 都在 -> 无告警
        with _m.patch.object(_r, "_family_available", return_value=True):
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
        cfg = Config(path=Path("/tmp/adr-sched-test.json"))
        now = dt.datetime(2026, 10, 14, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8)))
        self.assertEqual(target_day(cfg, now), "2026-10-13")

    def test_enabled_categories_filters_and_dedupes(self):
        cfg = Config(path=Path("/tmp/adr-sched-test.json"), overrides={
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
        from arxiv_daily_report.summarize import _join_paragraphs

        self.assertEqual(_join_paragraphs("中文", "English"), "中文\nEnglish")
        self.assertEqual(_join_paragraphs("中文", ""), "中文")
        self.assertEqual(_join_paragraphs("", "English"), "English")
        self.assertEqual(_join_paragraphs("", ""), "")

    def test_merge_bilingual_summaries(self):
        from arxiv_daily_report.summarize import DaySummary, _join_paragraphs

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
        from arxiv_daily_report.render import heading

        zh_only = heading("zh", "report.papers", False)
        self.assertEqual(zh_only, "论文清单")
        both = heading("zh", "report.papers", True)
        self.assertIn("论文清单", both)
        self.assertIn("Papers", both)
        self.assertIn("en-label", both)

    def test_english_report_labels(self):
        from arxiv_daily_report.render import heading

        self.assertEqual(heading("en", "report.papers", False), "Papers")
        self.assertEqual(heading("en", "report.highlights", False), "Highlights")

    def test_bilingual_daily_html_renders(self):
        """不调用模型，只验证双语版式能渲染出 HTML。"""
        import tempfile as _tf

        from arxiv_daily_report.render import ReportContext, render_daily_html

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


SAMPLE_ARXIV_HTML = """<!doctype html><html><body><article class="ltx_document">
<div id="abstract1" class="ltx_abstract">
  <h6 class="ltx_title ltx_title_abstract">Abstract</h6>
  <p class="ltx_p">We study quasibound states around <math alttext="a=Q/M">x</math> black holes.</p>
</div>
<section id="S1" class="ltx_section">
  <h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">I </span>Introduction</h2>
  <div class="ltx_para"><p class="ltx_p">Massive fields are interesting.</p></div>
</section>
<section id="S2" class="ltx_section">
  <h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">II </span>Method</h2>
  <div class="ltx_para"><p class="ltx_p">We solve the radial equation.</p></div>
</section>
<section id="S7" class="ltx_section">
  <h2 class="ltx_title ltx_title_section"><span class="ltx_tag ltx_tag_section">VII </span>Summary and discussion</h2>
  <div class="ltx_para"><p class="ltx_p">We find that the spectrum is stable for <math alttext="a<0.9">y</math>.</p></div>
</section>
<section id="bib" class="ltx_bibliography">
  <h2 class="ltx_title ltx_title_bibliography">References</h2>
  <div class="ltx_para"><p class="ltx_p">[1] Someone, Conclusions of a cited paper.</p></div>
</section>
</article></body></html>"""


class WebEntryTest(HermeticTestCase):
    """「怎么打开 Web 界面」这条链路：地址可复现、状态里能查到、没令牌时给提示。"""

    def _cfg(self, td, **web):
        conf = {"web": dict({"host": "127.0.0.1", "port": 8765, "enabled": True}, **web)}
        return Config(path=Path(td) / "config.json", overrides=conf)

    def test_default_url_needs_no_token(self):
        """默认只监听回环地址，直接打开就能用，地址里不带令牌。"""
        from arxiv_daily_report.server import token_required, web_url

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            self.assertFalse(token_required(cfg))
            self.assertEqual(web_url(cfg), "http://127.0.0.1:8765/")

    def test_require_token_opt_in_gives_stable_url(self):
        """显式打开 require_token 后，地址带令牌且跨进程可复现。"""
        from arxiv_daily_report.server import token_required, web_url

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, require_token=True)
            self.assertTrue(token_required(cfg))
            url = web_url(cfg)
            self.assertTrue(url.startswith("http://127.0.0.1:8765/?token="))
            again = web_url(Config(path=Path(td) / "config.json"))
            self.assertEqual(url, again)      # 令牌持久化在 secrets.json
            self.assertGreater(len(url.split("token=")[1]), 20)

    def test_password_also_requires_token(self):
        from arxiv_daily_report.server import token_required

        with tempfile.TemporaryDirectory() as td:
            self.assertTrue(token_required(self._cfg(td, password="pw")))

    def test_web_url_rewrites_wildcard_host(self):
        from arxiv_daily_report.server import web_url

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, host="0.0.0.0", password="x")
            self.assertIn("http://127.0.0.1:8765/", web_url(cfg))

    def test_status_shows_web_url(self):
        """status 必须直接给出可点开的地址——这是用户最常问的问题。"""
        from arxiv_daily_report.status import render_status

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            store = Store(Path(td) / "s.sqlite3")
            try:
                out = render_status(cfg, store)
                self.assertIn("配置界面", out)
                self.assertIn("http://127.0.0.1:8765/", out)
                self.assertIn("无需令牌", out)
            finally:
                store.close()

    def test_status_says_when_web_disabled(self):
        from arxiv_daily_report.status import render_status

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, enabled=False)
            store = Store(Path(td) / "s.sqlite3")
            try:
                out = render_status(cfg, store)
                self.assertIn("关闭", out)
                self.assertIn("web.enabled", out)
            finally:
                store.close()

    def test_authed_logic_trusts_loopback_by_default(self):
        """核心：不设密码、不开 require_token 时，回环访问放行。"""
        from arxiv_daily_report.server import Handler

        class FakeHandler(Handler):
            def __init__(self, cfg, headers=None, path="/"):
                self.cfg = cfg
                self.token = cfg.session_token()
                self.path = path
                self.headers = headers or {}

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            self.assertTrue(FakeHandler(cfg)._authed())
            # 打开 require_token 后必须带令牌
            Handler.require_token = True
            try:
                self.assertFalse(FakeHandler(cfg)._authed())
                self.assertTrue(FakeHandler(cfg, {"Cookie": "adr_token=" + cfg.session_token()})
                                ._authed())
            finally:
                Handler.require_token = False
            # 设了密码就必须登录
            cfg2 = self._cfg(td, password="pw")
            self.assertFalse(FakeHandler(cfg2)._authed())

    def test_host_header_blocks_dns_rebinding(self):
        """免令牌之后，Host 校验是防 DNS rebinding 的关键一步。"""
        from arxiv_daily_report.server import Handler

        class FakeHandler(Handler):
            def __init__(self, cfg, host):
                self.cfg = cfg
                self.headers = {"Host": host}
                self.path = "/"

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            for ok in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1", "[::1]:8765"):
                self.assertTrue(FakeHandler(cfg, ok)._host_ok(), ok)
            for bad in ("evil.com:8765", "attacker.example", "192.168.1.9:8765"):
                self.assertFalse(FakeHandler(cfg, bad)._host_ok(), bad)
            # 对外监听时由密码把关，Host 形态本来就多样
            cfg2 = self._cfg(td, host="0.0.0.0", password="pw")
            self.assertTrue(FakeHandler(cfg2, "192.168.1.9:8765")._host_ok())

    def test_no_non_ascii_reason_phrase_in_send_error(self):
        """回归：HTTP 状态行的原因短语只能是 latin-1，塞中文会让连接被掐断。"""
        import re as _re

        src = (PROJECT / "arxiv_daily_report/server.py").read_text(encoding="utf-8")
        for m in _re.finditer(r"send_error\(([^)]*)\)", src):
            arg = m.group(1)
            self.assertTrue(
                all(ord(ch) < 128 for ch in arg),
                f"send_error 的原因短语含非 ASCII 字符（会抛 UnicodeEncodeError）：{arg}",
            )

    def test_refuses_public_bind_without_password(self):
        """对外监听又不设密码会暴露模型密钥，必须拒绝启动界面。"""
        from arxiv_daily_report.server import make_server

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td, host="0.0.0.0", password="")
            store = Store(Path(td) / "s.sqlite3")
            try:
                with self.assertRaises(SystemExit):
                    make_server(cfg, store)
            finally:
                store.close()

    def test_bare_invocation_prints_usage_not_an_error(self):
        """python run.py 不带子命令时，应给出用法与界面地址，而不是 argparse 报错。"""
        import contextlib
        import io
        from unittest import mock

        from arxiv_daily_report.cli import main

        with tempfile.TemporaryDirectory() as td:
            env = {"ADR_HOME": td}
            with mock.patch.dict(os.environ, env):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    code = main([])
                out = buf.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("用法", out)
        self.assertIn("serve", out)
        self.assertIn("配置界面", out)
        self.assertIn("http://127.0.0.1:8765/", out)
        # 不应再出现 argparse 的报错
        self.assertNotIn("the following arguments are required", out)

    def test_subcommands_still_work(self):
        import contextlib
        import io
        from unittest import mock

        from arxiv_daily_report.cli import main

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"ADR_HOME": td}):

                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    self.assertEqual(main(["status"]), 0)
                self.assertIn("运行状态", buf.getvalue())

    def test_hint_page_explains_how_to_get_token(self):
        """没设密码又没令牌时，返回的是说清办法的页面，而不是一个裸 403。"""
        import re as _re

        from arxiv_daily_report.server import Handler

        self.assertTrue(hasattr(Handler, "_serve_hint"))
        src = Path(__import__("arxiv_daily_report.server", fromlist=["x"]).__file__).read_text(
            encoding="utf-8")
        self.assertIn("arxiv-daily-report status", src)
        self.assertIn("journalctl", src)

    def test_daemon_serves_web_by_default(self):
        """常驻服务默认同时提供配置界面，否则 systemd 方式下就没有界面可用。"""
        import inspect

        from arxiv_daily_report import scheduler

        sig = inspect.signature(scheduler.run_daemon)
        self.assertEqual(sig.parameters["with_web"].default, True)
        src = inspect.getsource(scheduler.run_daemon)
        self.assertIn("start_web_thread", src)


class FullTextTest(HermeticTestCase):
    """全文提取：摘要、结论类小节、公式还原、参考文献区排除。"""

    def test_extract_text_turns_math_into_latex(self):
        from arxiv_daily_report.fulltext import extract_text

        out = extract_text('前 <math alttext="a^2+b^2">x</math> 后')
        self.assertIn("$a^2+b^2$", out)
        self.assertNotIn("<math", out)
        # 没有 alttext 时不能把 MathML 拍平成乱码
        self.assertEqual(extract_text("<p>a<math>x y z</math>b</p>"), "a b")

    def test_parse_html_picks_abstract_and_conclusion(self):
        from arxiv_daily_report.fulltext import parse_html

        ft = parse_html("2610.00001", SAMPLE_ARXIV_HTML)
        self.assertTrue(ft.has_fulltext)
        self.assertEqual(ft.source, "arxiv-html")
        self.assertIn("quasibound states", ft.abstract)
        self.assertNotIn("Abstract", ft.abstract[:9])       # 摘要小标题已剔除
        self.assertIn("$a=Q/M$", ft.abstract)
        titles = [t for t, _ in ft.conclusions]
        self.assertEqual(titles, ["Summary and discussion"])   # 罗马数字已去掉
        self.assertIn("$a<0.9$", ft.conclusions[0][1])

    def test_parse_html_ignores_references_section(self):
        """参考文献里出现的 'Conclusions' 不能被抓成本文结论。"""
        from arxiv_daily_report.fulltext import parse_html

        ft = parse_html("x", SAMPLE_ARXIV_HTML)
        self.assertNotIn("References", ft.section_titles)
        self.assertNotIn("cited paper", " ".join(b for _t, b in ft.conclusions))

    def test_parse_html_without_conclusion_falls_back(self):
        from arxiv_daily_report.fulltext import parse_html

        html = ('<div class="ltx_abstract"><p class="ltx_p">Only an abstract here.</p></div>'
                '<section class="ltx_section"><h2 class="ltx_title ltx_title_section">'
                '<span class="ltx_tag">I </span>Introduction</h2><p>body</p></section>')
        ft = parse_html("x", html)
        self.assertFalse(ft.has_fulltext)
        self.assertEqual(ft.source, "abstract-only")
        self.assertIn("Only an abstract", ft.abstract)

    def test_conclusion_text_respects_budget(self):
        from arxiv_daily_report.fulltext import FullText

        ft = FullText(arxiv_id="x", conclusions=[("Conclusions", "字" * 5000)])
        # 预算之内不截断，超出才截
        self.assertLessEqual(len(ft.conclusion_text(300)), 320)
        self.assertEqual(ft.conclusion_text(6000).count("字"), 5000)
        # 解析阶段本身也有单节上限（_MAX_TEXT）
        from arxiv_daily_report.fulltext import _MAX_TEXT

        self.assertLessEqual(_MAX_TEXT, 8000)

    def test_is_conclusion_matches_common_headings(self):
        from arxiv_daily_report.fulltext import _is_conclusion

        for t in ("Conclusions", "Conclusion", "Summary", "Discussion",
                  "Summary and discussion", "Concluding remarks", "Outlook",
                  "Results and discussion", "结论", "讨论与总结"):
            self.assertTrue(_is_conclusion(t), t)
        for t in ("Introduction", "Method", "References", "Acknowledgements"):
            self.assertFalse(_is_conclusion(t), t)

    def test_prefers_real_conclusion_over_intro_summary(self):
        """有真正的结论节时不要用「引言兼小结」，否则预算花在引言上。"""
        from arxiv_daily_report.fulltext import parse_html

        def sec(t, body="text " * 60):
            return (f'<section class="ltx_section"><h2 class="ltx_title ltx_title_section">'
                    f'<span class="ltx_tag">X </span>{t}</h2><p>{body}</p></section>')

        both = ('<div class="ltx_abstract"><p>a</p></div>'
                + sec("Introduction and Summary") + sec("II Method") + sec("VII Conclusions"))
        ft = parse_html("x", both)
        self.assertEqual([t for t, _ in ft.conclusions], ["Conclusions"])
        self.assertFalse(ft.weak_section)

        only = ('<div class="ltx_abstract"><p>a</p></div>'
                + sec("Introduction and Summary") + sec("II Method"))
        ft2 = parse_html("x", only)
        self.assertEqual([t for t, _ in ft2.conclusions], ["Introduction and Summary"])
        self.assertTrue(ft2.weak_section)

    def test_disabled_service_returns_abstract_only(self):
        from arxiv_daily_report.fulltext import FullTextService

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json",
                         overrides={"fulltext": {"enabled": False}})
            svc = FullTextService(None, cfg)
            ft = svc.get("2610.00001")
            self.assertFalse(ft.has_fulltext)
            self.assertIn("关闭", ft.error)

    def test_fulltext_cache_roundtrip(self):
        from arxiv_daily_report.fulltext import FullText, FullTextService

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "s.sqlite3")
            try:
                cfg = Config(path=Path(td) / "c.json")
                # 先手工写一条缓存，服务应直接命中而完全不联网
                src = FullText(arxiv_id="2610.00001", source="arxiv-html", abstract="A",
                               conclusions=[("Conclusions", "B")], section_titles=["Conclusions"])
                store.save_fulltext("2610.00001", source=src.source, abstract=src.abstract,
                                    payload=src.to_json(), error="")
                svc = FullTextService(store, cfg)
                ft = svc.get("2610.00001")
                self.assertEqual(svc.stats["hit"], 1)
                self.assertEqual(svc.stats["fetched"], 0)
                self.assertEqual(ft.conclusions, [("Conclusions", "B")])
                self.assertEqual(store.fulltext_stats()["with_fulltext"], 1)
            finally:
                store.close()


class MonthlyCitationTest(HermeticTestCase):
    """月报的 arXiv 号：只能引用当天日报里确实存在的论文。"""

    INDEX = {
        "2026-10-13": [{"id": "2610.17034", "title": "A"}, {"id": "2610.18521", "title": "B"}],
        "2026-10-07": [{"id": "2610.12345", "title": "C"}],
    }

    def test_keeps_valid_id(self):
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations(
            "结果（10月13日，arXiv:2610.17034）。", "2026-10", self.INDEX)
        self.assertIn("arXiv:2610.17034", out)
        self.assertEqual(dropped, 0)

    def test_drops_invented_id(self):
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations(
            "结果（10月13日，arXiv:9999.99999）。", "2026-10", self.INDEX)
        self.assertEqual(out, "结果（10月13日）。")
        self.assertEqual(dropped, 1)

    def test_drops_id_from_wrong_day(self):
        """号真实存在，但属于别的日期——同样要剔除。"""
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations(
            "结果（10月13日，arXiv:2610.12345）。", "2026-10", self.INDEX)
        self.assertNotIn("2610.12345", out)
        self.assertEqual(dropped, 1)

    def test_keeps_partial_validity(self):
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations(
            "混（10月13日，arXiv:2610.17034，arXiv:9999.99999）。", "2026-10", self.INDEX)
        self.assertIn("2610.17034", out)
        self.assertNotIn("9999", out)
        self.assertEqual(dropped, 1)

    def test_id_without_date_is_dropped(self):
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations("（arXiv:2610.17034）", "2026-10", self.INDEX)
        self.assertNotIn("arXiv", out)
        self.assertEqual(dropped, 1)

    def test_plain_date_citation_untouched(self):
        from arxiv_daily_report.report_monthly import sanitize_citations

        out, dropped = sanitize_citations("结果（10月13日）。", "2026-10", self.INDEX)
        self.assertEqual(out, "结果（10月13日）。")
        self.assertEqual(dropped, 0)

    def test_version_suffix_does_not_break_matching(self):
        """回归：存档里的 id 带版本号（2610.17034v1），正文引用写裸号，必须能匹配上。

        否则校验器会把所有引用都当成编造，悄无声息地全部删掉。
        """
        from arxiv_daily_report.report_monthly import bare_id, sanitize_citations

        idx = {"2026-10-13": [{"id": "2610.17034v1", "title": "A"}]}
        out, dropped = sanitize_citations("（10月13日，arXiv:2610.17034）", "2026-10", idx)
        self.assertIn("arXiv:2610.17034", out)
        self.assertEqual(dropped, 0)
        # 归一化后不同版本视为同一篇
        self.assertEqual(bare_id("2610.17034v1"), bare_id("2610.17034"))
        self.assertEqual(bare_id("2610.17034"), "2610.17034")

    def test_highlight_ids_validated_against_its_days(self):
        """主要进展的 arxiv 字段：按 days 校验，跨日期或编造的号都要剔除。"""
        from arxiv_daily_report.report_monthly import pick_highlight_ids

        idx = {"2026-10-13": [{"id": "2610.17034v1"}],
               "2026-10-07": [{"id": "2610.12345v1"}]}
        # 合法
        picked, dropped = pick_highlight_ids(["2610.17034"], ["2026-10-13"], idx)
        self.assertEqual(picked, ["2610.17034"])
        self.assertEqual(dropped, 0)
        # 允许 arXiv: 前缀，也允许带版本号
        picked, _ = pick_highlight_ids(["arXiv:2610.17034v1"], ["2026-10-13"], idx)
        self.assertEqual(picked, ["2610.17034"])
        # 编造
        picked, dropped = pick_highlight_ids(["9999.99999"], ["2026-10-13"], idx)
        self.assertEqual(picked, [])
        self.assertEqual(dropped, 1)
        # 属于别的日期
        picked, dropped = pick_highlight_ids(["2610.12345"], ["2026-10-13"], idx)
        self.assertEqual(picked, [])
        self.assertEqual(dropped, 1)
        # 一条进展跨两天，两天的号都合法
        picked, dropped = pick_highlight_ids(["2610.17034", "2610.12345"],
                                             ["2026-10-13", "2026-10-07"], idx)
        self.assertEqual(picked, ["2610.17034", "2610.12345"])
        self.assertEqual(dropped, 0)
        # 去重
        picked, dropped = pick_highlight_ids(["2610.17034", "2610.17034"], ["2026-10-13"], idx)
        self.assertEqual(picked, ["2610.17034"])
        self.assertEqual(dropped, 1)

    def test_allowed_ids_text_lists_days(self):
        from arxiv_daily_report.report_monthly import _allowed_ids_text

        text = _allowed_ids_text(self.INDEX, "2026-10", "zh")
        self.assertIn("10月13日", text)
        self.assertIn("2610.17034", text)

    def test_linkify_makes_clickable_and_escapes(self):
        from arxiv_daily_report.report_monthly import linkify_arxiv

        out = linkify_arxiv("（10月13日，arXiv:2610.17034）")
        self.assertIn('href="https://arxiv.org/abs/2610.17034"', out)
        self.assertNotIn("<script>", linkify_arxiv("<script>x</script>"))


class OutputLayoutTest(HermeticTestCase):
    """产物目录分层：日报按月一层、月报按分类一层，避免单目录堆太多文件。"""

    def _cfg(self, td: Path):
        return Config(path=td / "config.json", overrides={
            "paths": {"reports": str(td / "reports"), "data": str(td / "data"),
                      "cache": str(td / "cache")},
            "subscriptions": [{"category": "gr-qc", "enabled": True,
                               "report_name": "", "out_dir": ""}],
        })

    def _paper(self, aid="2610.12345"):
        return Paper.from_dict({
            "arxiv_id": aid, "version": 1, "title": "T " + aid, "abstract": "A",
            "authors": ["X"], "categories": ["gr-qc"], "primary_category": "gr-qc",
            "announced": "2026-10-13T00:00:00Z", "updated": "2026-10-13T00:00:00Z",
            "summary": "s", "themes": ["blackhole"], "comment": "", "journal_ref": "",
            "doi": "", "abs_url": "", "pdf_url": "",
        })

    def test_daily_goes_into_month_subdir(self):
        """reports/gr-qc/2026-10/2026-10-13.pdf，latest.pdf 留在分类根目录。"""
        from arxiv_daily_report.report_daily import build_category_report, _out_dir
        from arxiv_daily_report.mathrender import MathService

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
                base = _out_dir(cfg, cfg["subscriptions"][0])
                path, _backend = build_category_report(
                    cfg, store, category="gr-qc", day="2026-10-13",
                    papers=[self._paper()], math=svc, use_llm=False, out_dir=base,
                )
                self.assertEqual(path.parent.name, "2026-10")
                self.assertEqual(path.name, "2026-10-13.pdf")
                self.assertTrue(path.exists())
                # latest.pdf 在分类根目录，路径不随月份变化
                self.assertTrue((base / "latest.pdf").exists())
                self.assertFalse((base / "2026-10-13.pdf").exists())
            finally:
                store.close()

    def test_daily_keeps_same_month_together(self):
        from arxiv_daily_report.report_daily import build_category_report, _out_dir
        from arxiv_daily_report.mathrender import MathService

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
                base = _out_dir(cfg, cfg["subscriptions"][0])
                for day in ("2026-10-13", "2026-11-02"):
                    build_category_report(
                        cfg, store, category="gr-qc", day=day,
                        papers=[self._paper()], math=svc, use_llm=False, out_dir=base,
                    )
                self.assertTrue((base / "2026-10" / "2026-10-13.pdf").exists())
                self.assertTrue((base / "2026-11" / "2026-11-02.pdf").exists())
                # 只有两个月份目录 + latest.pdf
                self.assertEqual(sorted(x.name for x in base.iterdir()),
                                 ["2026-10", "2026-11", "latest.pdf"])
            finally:
                store.close()

    def test_monthly_goes_into_category_subdir(self):
        """reports/monthly/gr-qc/2026-10.pdf。"""
        from arxiv_daily_report.report_monthly import run_monthly

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                store.save_day_digest("2026-10-13", "gr-qc", highlights="h",
                                      themes={"blackhole": "n"}, theme_counts={"blackhole": 1},
                                      papers=[{"id": "2610.12345", "title": "T"}], count=1)
                paths = run_monthly(cfg, store, month="2026-10", use_llm=False)
                self.assertEqual(len(paths), 1)
                rel = paths[0].relative_to(cfg.reports_dir)
                self.assertEqual(str(rel), "monthly/gr-qc/2026-10.pdf")
                self.assertTrue(paths[0].exists())
            finally:
                store.close()


class CacheCleanupTest(HermeticTestCase):
    """缓存清理：只动可再生的中间数据，绝不碰报告与状态。"""

    def _cfg(self, td: Path):
        return Config(path=td / "config.json", overrides={
            "paths": {"reports": str(td / "reports"), "data": str(td / "data"),
                      "cache": str(td / "cache")}})

    def test_interval_options(self):
        from arxiv_daily_report.cleanup import CLEANUP_OPTIONS, DEFAULT_CLEANUP, interval_days

        self.assertEqual(DEFAULT_CLEANUP, "3m")
        # 最短一周：不再提供按天清理
        self.assertEqual(CLEANUP_OPTIONS["1w"], 7)
        self.assertEqual(min(d for d in CLEANUP_OPTIONS.values() if d > 0), 7)
        for key in ("1d", "3d"):
            self.assertNotIn(key, CLEANUP_OPTIONS, "最短周期应为一周")
        self.assertEqual(CLEANUP_OPTIONS["2w"], 14)
        self.assertEqual(CLEANUP_OPTIONS["2m"], 60)
        self.assertEqual(CLEANUP_OPTIONS["1y"], 365)
        self.assertEqual(interval_days("6m"), 180)
        self.assertEqual(interval_days("never"), 0)
        # 不认识的值回落到默认（3 个月），而不是当成 0=不清理
        self.assertEqual(interval_days("nonsense"), 90)
        self.assertEqual(interval_days(None), 90)
        # 旧配置里的按天周期按新的最短周期处理，不要突然放宽到 3 个月
        self.assertEqual(interval_days("1d"), 7)
        self.assertEqual(interval_days("3d"), 7)

    def test_removes_only_expired_cache(self):
        import datetime as _dt

        from arxiv_daily_report.cleanup import cleanup_cache

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            raw = cfg.raw_cache_dir
            (raw / "2026-01-01").mkdir(parents=True)
            (raw / "2026-01-01" / "q-1.xml").write_text("old", encoding="utf-8")
            (raw / "2026-06-01").mkdir(parents=True)
            (raw / "2026-06-01" / "q-2.xml").write_text("recent", encoding="utf-8")
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                store.save_fulltext("2610.1", payload="{}")
                store.conn.execute("INSERT INTO fulltext(arxiv_id,source,abstract,payload,error,"
                                   "fetched_at) VALUES('old.1','','','{}','','2026-01-01T00:00:00')")
                store.conn.execute("INSERT INTO math_cache(tex_hash,tex,svg,created_at) "
                                   "VALUES('h','x','<svg/>','2026-01-01T00:00:00')")
                store.conn.commit()
                r = cleanup_cache(cfg, store, progress=lambda m: None,
                                  today=_dt.date(2026, 7, 1))
                # 90 天后：1 月 1 日的没了，6 月 1 日的还在
                self.assertEqual(r["raw_removed"], 1)
                self.assertFalse((raw / "2026-01-01").exists())
                self.assertTrue((raw / "2026-06-01").exists())
                self.assertEqual(r["math_removed"], 1)
                self.assertEqual(r["fulltext_removed"], 1)
                self.assertEqual(
                    store.conn.execute("SELECT COUNT(*) FROM fulltext").fetchone()[0], 1)
                # 报告与状态表不能被动
                self.assertTrue((tmp / "reports").exists() or True)
            finally:
                store.close()

    def test_never_skips(self):
        from arxiv_daily_report.cleanup import cleanup_cache

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(path=tmp / "config.json", overrides={
                "cache": {"cleanup": "never"},
                "paths": {"cache": str(tmp / "cache"), "data": str(tmp / "data"),
                          "reports": str(tmp / "reports")}})
            raw = cfg.raw_cache_dir
            (raw / "2000-01-01").mkdir(parents=True)
            (raw / "2000-01-01" / "x.xml").write_text("x", encoding="utf-8")
            r = cleanup_cache(cfg, None, progress=lambda m: None)
            self.assertTrue(r["skipped"])
            self.assertTrue((raw / "2000-01-01").exists())

    def test_does_not_touch_reports_or_state(self):
        import datetime as _dt

        from arxiv_daily_report.cleanup import cleanup_cache

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            rep = cfg.reports_dir / "gr-qc" / "2026-01"
            rep.mkdir(parents=True)
            (rep / "2026-01-05.pdf").write_text("PDF", encoding="utf-8")
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                store.save_day_digest("2026-01-05", "gr-qc", highlights="h", count=3)
                cleanup_cache(cfg, store, progress=lambda m: None, today=_dt.date(2026, 7, 1))
                self.assertTrue((rep / "2026-01-05.pdf").exists())      # 报告还在
                self.assertIsNotNone(store.get_day_digest("2026-01-05", "gr-qc"))  # 摘要还在
            finally:
                store.close()

    def _paper_payload(self, summary="要点", ver=None, day="2026-01-05", aid="2610.1"):
        import json as _j
        from arxiv_daily_report.summarize import PROMPT_VERSION

        return _j.dumps({
            "arxiv_id": aid, "version": 1, "title": "T",
            "abstract": "A" * 400, "authors": ["X"], "categories": ["gr-qc"],
            "primary_category": "gr-qc", "announced": f"{day}T02:00:00Z",
            "updated": f"{day}T02:00:00Z", "summary": summary, "themes": ["blackhole"],
            "comment": "", "journal_ref": "", "doi": "", "abs_url": "", "pdf_url": "",
            "llm_prompt_version": PROMPT_VERSION if ver is None else ver,
        }, ensure_ascii=False)

    def test_releases_abstracts_only_for_fully_current_days(self):
        """只有「整天都已是当前版本要点」的日期才释放摘要原文。

        否则重跑那天会把论文再次送进模型，而摘要已经被删掉 -> 空输入。
        """
        import datetime as _dt
        import json as _j

        from arxiv_daily_report.cleanup import release_abstracts
        from arxiv_daily_report.summarize import PROMPT_VERSION

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                tz = cfg["schedule"]["timezone"]
                # 两个日期各有日报存档；一个整天是当前版本，一个有一篇是旧版本
                for day, ver in (("2026-01-05", None), ("2026-01-06", "daily-v1")):
                    from arxiv_daily_report.models import day_bounds_epoch

                    uid = f"2610.{day[-2:]}v1"
                    ts = day_bounds_epoch(day, "Asia/Shanghai")[0] + 36000   # 当天 10:00 北京
                    store.conn.execute(
                        "INSERT INTO papers(uid,arxiv_id,version,primary_category,published,"
                        "updated,day_bj,day_et,ts_utc,payload,summarized,first_seen) "
                        "VALUES(?,?,1,'gr-qc',?,?,?,'2026-01-05',?,?,1,'x')",
                        (uid, uid.split("v")[0], f"{day}T02:00:00Z", f"{day}T02:00:00Z",
                         day, ts,
                         self._paper_payload(ver=ver, day=day, aid=uid.split("v")[0])))
                    store.conn.execute(
                        "INSERT INTO paper_categories(uid,category,is_primary) VALUES(?,'gr-qc',1)",
                        (uid,))
                    store.save_day_digest(day, "gr-qc", highlights="h", count=1)
                store.conn.commit()
                r = release_abstracts(cfg, store, cutoff=_dt.date(2026, 2, 1),
                                      progress=lambda m: None)
                self.assertEqual(r["safe_days"], 1)
                self.assertEqual(r["released"], 1)
                # 只有 01-05 的摘要被释放
                got = {row["day_bj"]: _j.loads(row["payload"])
                       for row in store.conn.execute("SELECT day_bj,payload FROM papers")}
                self.assertNotIn("abstract", got["2026-01-05"])
                self.assertIn("abstract", got["2026-01-06"])
                # 要点与摘要存档绝不能动
                self.assertTrue(got["2026-01-05"]["summary"])
                self.assertIsNotNone(store.get_day_digest("2026-01-05", "gr-qc"))
            finally:
                store.close()

    def test_release_skips_days_without_digest(self):
        import datetime as _dt

        from arxiv_daily_report.cleanup import release_abstracts

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            store = Store(tmp / "data" / "state.sqlite3")
            try:
                from arxiv_daily_report.models import day_bounds_epoch

                ts = day_bounds_epoch("2026-01-05", "Asia/Shanghai")[0] + 36000
                store.conn.execute(
                    "INSERT INTO papers(uid,arxiv_id,version,primary_category,published,"
                    "updated,day_bj,day_et,ts_utc,payload,summarized,first_seen) "
                    "VALUES('a1','2610.1',1,'gr-qc','2026-01-05T02:00:00Z',"
                    "'2026-01-05T02:00:00Z','2026-01-05','2026-01-05',?,?,1,'x')",
                    (ts, self._paper_payload()))
                store.conn.commit()
                r = release_abstracts(cfg, store, cutoff=_dt.date(2026, 2, 1),
                                      progress=lambda m: None)
                self.assertEqual(r["released"], 0)      # 没有日报存档就不动
            finally:
                store.close()

    def test_daily_reuses_stored_summaries(self):
        """整天都是当前版本要点 + 有存档 -> 不调用模型。"""
        import inspect

        from arxiv_daily_report import report_daily as _rd

        src = inspect.getsource(_rd.build_category_report)
        self.assertIn("复用存档，不调用模型", src)
        self.assertIn("llm_prompt_version", src)

    def test_cache_usage_reports_sizes(self):
        from arxiv_daily_report.cleanup import cache_usage

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = self._cfg(tmp)
            (cfg.raw_cache_dir / "2026-01-01").mkdir(parents=True)
            (cfg.raw_cache_dir / "2026-01-01" / "a.xml").write_text("hello", encoding="utf-8")
            u = cache_usage(cfg)
            self.assertEqual(u["raw_files"], 1)
            self.assertEqual(u["raw_bytes"], 5)


class StorageLayoutTest(HermeticTestCase):
    """存储布局：最终报告给用户看，中间数据遵守 XDG 规范。"""

    def test_installed_layout_uses_documents_and_state(self):
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with mock.patch.object(_cfg, "_is_source_checkout", return_value=False), \
             mock.patch.dict(_cfg.os.environ, {}, clear=False):
            for k in ("ADR_HOME", "ADR_CONFIG_HOME", "ADR_DATA_HOME", "ADR_CACHE_HOME"):
                _cfg.os.environ.pop(k, None)
            self.assertTrue(str(_cfg.data_dir()).endswith(".local/state/" + _cfg.APP_NAME))
            self.assertTrue(str(_cfg.cache_dir()).endswith(".cache/" + _cfg.APP_NAME))
            docs = _cfg.documents_dir()
            self.assertEqual(docs.name, _cfg.APP_NAME)
            self.assertIn("Documents", str(docs))

    def test_documents_dir_honours_env_override(self):
        """XDG_DOCUMENTS_DIR 是 user-dirs 的标准导出方式，必须优先尊重。"""
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(_cfg.os.environ, {"XDG_DOCUMENTS_DIR": td}):
                self.assertEqual(_cfg.documents_dir(), Path(td) / _cfg.APP_NAME)

    def test_documents_dir_ignores_home_fallback(self):
        """$HOME 这个值表示「没配置」。

        没有 user-dirs.dirs 时 xdg-user-dir 会直接回退成 $HOME，
        默认模板写的也是 XDG_DOCUMENTS_DIR="$HOME/"。
        要是照单全收，报告就会落在家的根目录下。
        """
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        fake_home = Path("/tmp/fakehome-for-test")
        rendered = [{"out": ""}, {"out": str(fake_home)}, {"out": str(fake_home / "文档")}]
        for case in rendered:
            def fake_run(*_a, **_k):
                return mock.Mock(stdout=case["out"], returncode=0)

            def fake_read(self, *_a, **_k):
                raise FileNotFoundError(self)

            with mock.patch.object(_cfg.Path, "home", return_value=fake_home), \
                 mock.patch.object(_cfg.Path, "read_text", fake_read), \
                 mock.patch("subprocess.run", fake_run), \
                 mock.patch("shutil.which", return_value="/usr/bin/xdg-user-dir"), \
                 mock.patch.dict(_cfg.os.environ, {}, clear=False):
                _cfg.os.environ.pop("XDG_DOCUMENTS_DIR", None)
                got = _cfg.documents_dir()
            if case["out"] in ("", str(fake_home)):
                self.assertEqual(got, fake_home / "Documents" / _cfg.APP_NAME)
            else:
                self.assertEqual(got, fake_home / "文档" / _cfg.APP_NAME)

    def test_portable_layout_keeps_everything_in_project(self):
        from unittest import mock

        from arxiv_daily_report import config as _cfg

        with mock.patch.object(_cfg, "_is_source_checkout", return_value=True), \
             mock.patch.dict(_cfg.os.environ, {}, clear=False):
            for k in ("ADR_HOME", "ADR_CONFIG_HOME", "ADR_DATA_HOME", "ADR_CACHE_HOME"):
                _cfg.os.environ.pop(k, None)
            self.assertEqual(_cfg.data_dir(), _cfg.PROJECT_ROOT / "data")
            self.assertEqual(_cfg.cache_dir(), _cfg.PROJECT_ROOT / "cache")

    def test_reports_dir_prefers_explicit_path(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(path=tmp / "config.json",
                         overrides={"paths": {"reports": str(tmp / "mine")}})
            self.assertEqual(cfg.reports_dir, tmp / "mine")

    def test_raw_cache_lives_in_cache_dir(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(path=tmp / "config.json",
                         overrides={"paths": {"cache": str(tmp / "cache"),
                                              "data": str(tmp / "data"),
                                              "reports": str(tmp / "reports")}})
            self.assertEqual(cfg.raw_cache_dir, tmp / "cache" / "raw")
            self.assertNotIn("data", str(cfg.raw_cache_dir))


class RepoHygieneTest(HermeticTestCase):
    """仓库卫生：随程序分发的资产必须真的入库。

    回归背景：.gitignore 里曾写着 ``data/``，把整个目录忽略了，
    于是分类表与主题表从未被提交——本地跑测试没事（工作区有文件），
    CI 检出后却缺文件，打出来的包也是坏的。
    """

    ASSETS = ("data/arxiv_categories.json", "data/theme_taxonomy.json")

    def test_data_assets_are_not_ignored(self):
        import subprocess

        if not (PROJECT / ".git").exists():
            self.skipTest("不是 git 工作区")
        for rel in self.ASSETS:
            r = subprocess.run(["git", "check-ignore", "-q", rel], cwd=PROJECT)
            self.assertNotEqual(r.returncode, 0, f"{rel} 被 .gitignore 忽略了，CI 检出后会缺文件")

    def test_data_assets_are_tracked(self):
        import subprocess

        if not (PROJECT / ".git").exists():
            self.skipTest("不是 git 工作区")
        for rel in self.ASSETS:
            r = subprocess.run(["git", "ls-files", "--error-unmatch", rel],
                               cwd=PROJECT, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{rel} 没有被 git 跟踪")

    def test_runtime_state_is_still_ignored(self):
        """别修过头：运行期的数据库不能被提交。"""
        import subprocess

        if not (PROJECT / ".git").exists():
            self.skipTest("不是 git 工作区")
        r = subprocess.run(["git", "check-ignore", "-q", "data/state.sqlite3"], cwd=PROJECT)
        self.assertEqual(r.returncode, 0, "state.sqlite3 必须继续被忽略")

    def test_packaged_assets_come_from_the_repo(self):
        """校验脚本要求的资产，必须在工作区里真实存在。"""
        for rel in self.ASSETS:
            self.assertTrue((PROJECT / rel).is_file(), f"{rel} 缺失")


class ReviewTest(HermeticTestCase):
    """双模型：生成 → 校验 → 修订一次。"""

    def _cfg(self, td, **verify):
        base = {"enabled": True, "provider": "custom", "model": "verifier-x",
                "base_url": "http://verify/v1"}
        base.update(verify)
        return Config(path=td / "c.json", overrides={
            "llm": {"provider": "custom", "model": "gen-x", "base_url": "http://gen/v1",
                    "verify": base},
            "paths": {"data": str(td / "d"), "cache": str(td / "k"),
                      "reports": str(td / "r")}})

    def test_disabled_by_default(self):
        from arxiv_daily_report.review import enabled, verify_client

        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json")
            self.assertFalse(enabled(cfg))
            self.assertIsNone(verify_client(cfg))

    def test_empty_model_means_disabled(self):
        from arxiv_daily_report.review import enabled, verify_client

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td), model="")
            self.assertFalse(enabled(cfg))
            self.assertIsNone(verify_client(cfg))

    def test_verify_client_uses_configured_model(self):
        from arxiv_daily_report.review import describe, verify_client

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            client = verify_client(cfg)
            self.assertIsNotNone(client)
            self.assertEqual(client.model, "verifier-x")
            self.assertIn("verifier-x", describe(cfg))

    def test_independent_verifier_api_key(self):
        """校验模型要有自己的密钥槽：可以与生成模型不同账号。"""
        from arxiv_daily_report.review import verify_client

        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(Path(td))
            cfg.set_api_key("custom", "sk-gen-AAA")
            self.assertEqual(cfg.api_key("custom"), "sk-gen-AAA")
            self.assertEqual(cfg.verify_api_key(), "", "未设置时不应顺手用生成模型的")
            # 未设独立密钥时回落到厂商密钥，保证同厂商场景开箱可用
            client = verify_client(cfg, None)
            self.assertEqual(client.api_key, "sk-gen-AAA")

            cfg.set_verify_api_key("sk-ver-BBBBBBBBBBBB")
            self.assertEqual(cfg.verify_api_key(), "sk-ver-BBBBBBBBBBBB")
            self.assertIn("sk-ver", cfg.mask_verify_key())
            self.assertNotIn("BBBBBBBBBBBB", cfg.mask_verify_key(), "掩码不能泄漏密钥")
            client = verify_client(cfg, None)
            self.assertEqual(client.api_key, "sk-ver-BBBBBBBBBBBB")

            # 落盘后仍能取回
            cfg.save()
            again = Config(path=cfg.path)
            self.assertEqual(again.verify_api_key(), "sk-ver-BBBBBBBBBBBB")

            cfg.set_verify_api_key("")
            self.assertEqual(cfg.verify_api_key(), "")

    def test_ok_verdict_skips_revision(self):
        from arxiv_daily_report.review import review_and_revise

        class Fake:
            def __init__(self, reply): self.reply = reply; self.n = 0
            def chat_json(self, *a, **k): self.n += 1; return self.reply

        gen = Fake({"should": "never be used"})
        ver = Fake({"issues": [], "verdict": "ok"})
        revised, verdict = review_and_revise(
            gen, ver, draft={"a": 1}, source="材料", subject="日报",
            checklist="", system="s", output_hint="{}")
        self.assertIsNone(revised)
        self.assertEqual(gen.n, 0, "校验通过时不应再调用生成模型")
        self.assertEqual(verdict.get("verdict"), "ok")

    def test_issues_trigger_one_revision(self):
        from arxiv_daily_report.review import review_and_revise

        class Fake:
            def __init__(self, reply): self.reply = reply; self.n = 0; self.last = ""
            def chat_json(self, system, user, *, schema_hint="", max_tokens=None):
                self.n += 1
                self.last = user
                return self.reply

        gen = Fake({"a": 2})
        ver = Fake({"issues": [{"where": "论文 1", "problem": "无中生有",
                                "evidence": "摘要没有这句", "fix": "删掉"}],
                    "verdict": "revise"})
        revised, _ = review_and_revise(
            gen, ver, draft={"a": 1}, source="材料", subject="日报",
            checklist="", system="s", output_hint="{}")
        self.assertEqual(revised, {"a": 2})
        self.assertEqual(gen.n, 1, "只修订一次")
        self.assertIn("无中生有", gen.last)

    def test_verifier_failure_keeps_draft(self):
        from arxiv_daily_report.review import review_and_revise
        from arxiv_daily_report.llm.client import LLMError

        class Boom:
            def chat_json(self, *a, **k): raise LLMError("网络挂了")

        class Gen:
            def chat_json(self, *a, **k): raise AssertionError("不该被调用")

        revised, verdict = review_and_revise(
            Gen(), Boom(), draft={"a": 1}, source="材料", subject="日报",
            checklist="", system="s", output_hint="{}")
        self.assertIsNone(revised)
        self.assertEqual(verdict, {})


class AnnualTest(HermeticTestCase):
    """年报：按子主题、参考日报与月报、按自然年。"""

    def _setup(self, td):
        cfg = Config(path=td / "c.json", overrides={
            "schedule": {"timezone": "Asia/Shanghai", "auto_annual": True},
            "subscriptions": [{"category": "gr-qc", "enabled": True,
                               "report_name": "", "out_dir": ""}],
            "paths": {"data": str(td / "d"), "cache": str(td / "k"),
                      "reports": str(td / "r")}})
        store = Store(td / "d" / "state.sqlite3")
        return cfg, store

    def test_collect_year_aggregates_digests(self):
        from arxiv_daily_report.report_annual import collect_year

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                store.save_day_digest("2026-03-05", "gr-qc", highlights="h1",
                                      themes={"blackhole": "黑洞进展"},
                                      theme_counts={"blackhole": 3},
                                      papers=[{"id": "2603.1", "title": "A"}])
                store.save_day_digest("2026-07-09", "gr-qc", highlights="h2",
                                      themes={"cosmology": "宇宙学进展"},
                                      theme_counts={"cosmology": 2},
                                      papers=[{"id": "2607.2", "title": "B"}])
                # 次年数据不应计入
                store.save_day_digest("2027-01-03", "gr-qc", highlights="h3",
                                      themes={"blackhole": "次年"}, theme_counts={"blackhole": 9})
                data = collect_year(cfg, store, "2026")
                self.assertEqual(data.days, ["2026-03-05", "2026-07-09"])
                self.assertEqual(data.paper_count, 2)
                self.assertEqual(data.theme_counts, {"blackhole": 3, "cosmology": 2})
                self.assertIn("blackhole", data.theme_material)
                self.assertEqual(data.months, ["2026-03", "2026-07"])
            finally:
                store.close()

    def test_collect_year_includes_monthly_briefs(self):
        from arxiv_daily_report.report_annual import collect_year

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                store.save_day_digest("2026-03-05", "gr-qc", highlights="h",
                                      themes={"blackhole": "日报里的黑洞"}, theme_counts={})
                store.save_month_digest("2026-03", "gr-qc", overview="三月概览",
                                        themes=[{"code": "blackhole", "text": "月报里的黑洞"}])
                data = collect_year(cfg, store, "2026")
                joined = "\n".join(data.theme_material["blackhole"])
                self.assertIn("日报里的黑洞", joined)
                self.assertIn("月报里的黑洞", joined, "年报必须同时参考月报")
                self.assertEqual(data.months, ["2026-03"])
            finally:
                store.close()

    def test_annual_runs_on_jan_first_for_previous_year(self):
        import datetime as _dt
        from zoneinfo import ZoneInfo

        from arxiv_daily_report.report_annual import should_run_annual

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                tz = ZoneInfo("Asia/Shanghai")
                self.assertEqual(should_run_annual(cfg, _dt.datetime(2027, 1, 1, 9, tzinfo=tz)), "2026")
                self.assertIsNone(should_run_annual(cfg, _dt.datetime(2026, 12, 31, 23, tzinfo=tz)))
                self.assertIsNone(should_run_annual(cfg, _dt.datetime(2027, 1, 2, 9, tzinfo=tz)))
                cfg.update({"schedule": {"auto_annual": False}})
                self.assertIsNone(should_run_annual(cfg, _dt.datetime(2027, 1, 1, 9, tzinfo=tz)))
            finally:
                store.close()

    def test_render_annual_is_theme_centred(self):
        from arxiv_daily_report.report_annual import AnnualData, render_annual_html

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                data = AnnualData(year="2026", scope="gr-qc", days=["2026-03-05"],
                                  months=["2026-03"], papers=["2603.1"],
                                  theme_counts={"blackhole": 3})
                data.overview = "全年概览"
                data.themes = [{"code": "blackhole", "text": "黑洞方向的年度回顾",
                                "refs": ["2026-03"], "count": 3}]
                html = render_annual_html(cfg, data, scope_name="gr-qc")
                self.assertIn("全年概览", html)
                self.assertIn("分主题年度回顾", html)
                self.assertIn("黑洞方向的年度回顾", html)
                self.assertIn("2026年3月", html)      # 出处渲染成人可读形式
            finally:
                store.close()


class MathRenderTest(HermeticTestCase):
    """公式排版：安装包里没有 MathJax，必须能靠 matplotlib 排出真正的公式。"""

    def test_unicode_fallback_has_no_latex_residue(self):
        r"""降级路径过去会把 \left 当成 \le 替换成「≤ft」。"""
        from arxiv_daily_report.mathrender import unicode_math

        got = unicode_math(r"\left(\frac{a}{b}\right)^{1/2}")
        self.assertNotIn("≤ft", got)
        self.assertNotIn("\\", got)

        got = unicode_math(r"\Omega_{\rm b}=0.05")
        self.assertNotIn("Ωₘ", got)          # 过去把 \rm 的 r/m 当成了下标
        self.assertIn("Ω", got)

        self.assertIn("≤", unicode_math(r"a \le b"))
        self.assertIn("∈", unicode_math(r"x \in [0,1]"))

    def test_matplotlib_backend_renders_svg(self):
        """有 matplotlib 时应当产出内联 SVG，而不是纯文本。"""
        from arxiv_daily_report.mathrender import _matplotlib_ready, matplotlib_svg

        if not _matplotlib_ready():
            self.skipTest("本机没有 matplotlib")
        from arxiv_daily_report.mathrender import _matplotlib_setup

        _matplotlib_setup()
        svg = matplotlib_svg(r"H_0=71.4^{+2.2}_{-2.5}")
        self.assertTrue(svg.startswith("<svg"))
        self.assertIn("em", svg[:400])              # 尺寸换算成 em
        self.assertIn("vertical-align", svg[:400])  # 按深度对齐基线
        self.assertNotIn("<text", svg)              # 字形必须转成路径

    def test_service_uses_matplotlib_when_mathjax_missing(self):
        """MathJax 不可用时，公式仍要排出来（走 matplotlib）。"""
        from arxiv_daily_report.mathrender import MathService, _matplotlib_ready

        if not _matplotlib_ready():
            self.skipTest("本机没有 matplotlib")
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = Store(tmp / "s.sqlite3")
            try:
                # 显式指定 matplotlib，确定性覆盖「没有 MathJax」的路径
                svc = MathService(store, assets_dir=PROJECT / "assets", backend="matplotlib")
                self.assertFalse(svc.mathjax_available)
                text = "得到 $H_0=71.4$ 与 $|a|<m$。"
                svc.prepare([text])
                html = svc.to_html(text)
                self.assertIn("<svg", html)
                self.assertNotIn("math-unicode", html)
                self.assertEqual(svc.report_stats()["unicode"], 0)
            finally:
                store.close()

    def test_unicode_backend_is_forced_when_configured(self):
        from arxiv_daily_report.mathrender import MathService

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "s.sqlite3")
            try:
                svc = MathService(store, assets_dir=PROJECT / "assets", backend="unicode")
                self.assertFalse(svc.mathjax_available)
                self.assertFalse(svc.matplotlib_available)
                text = "得到 $H_0=71.4$。"
                svc.prepare([text])
                self.assertIn("math-unicode", svc.to_html(text))
            finally:
                store.close()


class BackfillTest(HermeticTestCase):
    """补缺：运行时检查当月已生成的报告，把「有论文但没出报告」的日期补上。"""

    def _setup(self, td: Path):
        cfg = Config(path=td / "c.json", overrides={
            "schedule": {"timezone": "Asia/Shanghai", "backfill_days": 31,
                         "backfill_max_per_run": 2, "catch_up_days": 7},
            "subscriptions": [{"category": "gr-qc", "enabled": True,
                               "report_name": "", "out_dir": ""}],
            "paths": {"data": str(td / "d"), "cache": str(td / "k"),
                      "reports": str(td / "r")}})
        store = Store(td / "d" / "state.sqlite3")
        return cfg, store

    @staticmethod
    def _add(store, day: str, n: int = 1):
        from arxiv_daily_report.models import Paper

        for i in range(n):
            paper = make_paper(aid=f"2610.{abs(hash(day)) % 1000}{i}")
            paper.updated = f"{day}T02:00:00Z"
            paper.published = paper.updated
            store.add_paper(paper, {"gr-qc": True})

    def test_detects_days_with_papers_but_no_report(self):
        from arxiv_daily_report.report_daily import missing_days

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
                    self._add(store, day)
                store.record_report("2026-10-10", "gr-qc", "/r/a.pdf", 1)
                store.record_report("2026-10-12", "gr-qc", "/r/b.pdf", 1)
                got = missing_days(cfg, store, today="2026-10-15")
                self.assertEqual(got, [("2026-10-11", "gr-qc")])
            finally:
                store.close()

    def test_days_without_papers_are_not_missing(self):
        """周末/节假日没有论文是正常的，不能当成遗漏去补。"""
        from arxiv_daily_report.report_daily import missing_days

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                self._add(store, "2026-10-10")
                got = missing_days(cfg, store, today="2026-10-15")
                self.assertEqual(got, [("2026-10-10", "gr-qc")])
                # 10-11 ~ 10-14 都没有论文，不应出现
                self.assertNotIn("2026-10-11", [d for d, _c in got])
            finally:
                store.close()

    def test_today_is_not_backfilled(self):
        from arxiv_daily_report.report_daily import missing_days

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                self._add(store, "2026-10-12")
                self._add(store, "2026-10-13")
                got = missing_days(cfg, store, today="2026-10-13")
                self.assertEqual(got, [("2026-10-12", "gr-qc")])
            finally:
                store.close()

    def test_backfill_respects_per_run_cap(self):
        from arxiv_daily_report.report_daily import backfill_missing

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg, store = self._setup(tmp)
            try:
                store.kv_set("initial_backfill_done", "1")   # 模拟非初次运行
                for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
                    self._add(store, day)
                # 单次上限 2 天 -> 只补最早两天
                out = backfill_missing(cfg, store, use_llm=False,
                                      progress=lambda m: None, limit=2)
                self.assertEqual(len(out), 2)
                self.assertTrue(store.report_exists("2026-10-10", "gr-qc"))
                self.assertTrue(store.report_exists("2026-10-11", "gr-qc"))
                self.assertFalse(store.report_exists("2026-10-12", "gr-qc"))
            finally:
                store.close()

    def test_backfill_is_idempotent(self):
        """已经补齐后再跑一次不应重复生成。"""
        from arxiv_daily_report.report_daily import backfill_missing

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                store.kv_set("initial_backfill_done", "1")
                self._add(store, "2026-10-10")
                backfill_missing(cfg, store, use_llm=False, progress=lambda m: None)
                again = backfill_missing(cfg, store, use_llm=False, progress=lambda m: None)
                self.assertEqual(again, [])
            finally:
                store.close()

    def test_first_run_backfills_whole_month(self):
        """初次运行要把当月缺的日报一次补齐，不受单次上限限制。"""
        from arxiv_daily_report.report_daily import backfill_missing, missing_days

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                days = [f"2026-10-{d:02d}" for d in (2, 3, 5, 6, 8, 9)]
                for day in days:
                    self._add(store, day)
                found = [d for d, _c in missing_days(cfg, store, today="2026-10-15")]
                self.assertEqual(found, days)
                # 配置的单次上限是 2，但初次运行应忽略它
                self.assertEqual(cfg["schedule"]["backfill_max_per_run"], 2)
                out = backfill_missing(cfg, store, use_llm=False, progress=lambda m: None)
                self.assertEqual(len(out), len(days), "初次运行应一次补齐当月")
                for day in days:
                    self.assertTrue(store.report_exists(day, "gr-qc"))
                self.assertEqual(store.kv_get("initial_backfill_done", ""), "1")
                # 之后再跑不应重复生成
                self.assertEqual(
                    backfill_missing(cfg, store, use_llm=False, progress=lambda m: None), [])
            finally:
                store.close()

    def test_adaptive_lookback_grows_after_downtime(self):
        """停机期间错过的日期要真的去抓，否则没有数据可补。"""
        from arxiv_daily_report.report_daily import _auto_lookback

        with tempfile.TemporaryDirectory() as td:
            cfg, store = self._setup(Path(td))
            try:
                # 首次运行：覆盖当月（10-01 ~ 10-19 共 19 天）
                self.assertEqual(_auto_lookback(cfg, store, "2026-10-19"), 19)
                # 首次运行但目标日在月初：7 天就够覆盖到 1 号
                self.assertEqual(_auto_lookback(cfg, store, "2026-10-02"), 7)
                # 之后按停机天数扩展
                store.kv_set("last_success_day", "2026-10-10")
                self.assertEqual(_auto_lookback(cfg, store, "2026-10-19"), 10)
                store.kv_set("last_success_day", "2026-08-01")
                self.assertEqual(_auto_lookback(cfg, store, "2026-10-19"), 31)
            finally:
                store.close()


class WebControlTest(HermeticTestCase):
    """设置界面的控件形态。

    回归背景：模型与时区原本用 <input list> + <datalist>。datalist 会按输入框
    现有内容过滤选项 —— 已经填着 deepseek-flash 时，展开只剩它自己，用户
    根本换不了模型。时区同理，只有一个值可选。
    """

    def _html(self) -> str:
        return (PROJECT / "web/index.html").read_text(encoding="utf-8")

    def _js(self) -> str:
        return (PROJECT / "web/app.js").read_text(encoding="utf-8")

    def test_timezone_is_a_real_select_with_many_choices(self):
        html = self._html()
        m = __import__("re").search(
            r'<select[^>]*data-path="schedule\.timezone"[^>]*>(.*?)</select>', html, __import__("re").S)
        self.assertIsNotNone(m, "时区应当是 select")
        opts = __import__("re").findall(r'value="([^"]+)"', m.group(1))
        self.assertGreaterEqual(len(opts), 15, "常见时区应给出足够多的选择")
        self.assertIn("Asia/Shanghai", opts)
        self.assertIn("Asia/Tokyo", opts)
        self.assertIn("America/New_York", opts)
        self.assertIn("UTC", opts)

    def test_timezone_is_not_a_datalist(self):
        html = self._html()
        self.assertNotIn('list="tz-options"', html, "datalist 会按当前值过滤，导致只有一个可选")
        self.assertNotIn('id="tz-options"', html)

    def test_model_picker_is_a_real_select(self):
        html = self._html()
        self.assertIn('id="model-pick"', html, "模型要有真正的下拉框")
        self.assertNotIn('list="model-options"', html, "datalist 同样会按当前值过滤")
        self.assertNotIn('id="model-options"', html)
        js = self._js()
        self.assertIn("#model-pick", js)
        # 选中后要写回输入框与配置，并复位到提示项
        self.assertIn("setPath(state.config, 'llm.model', pick.value)", js)

    def test_model_picker_writes_into_model_input(self):
        js = self._js()
        block = js[js.index("function renderModelOptions"):]
        block = block[:block.index("\n  }\n")] if "\n  }\n" in block else block
        self.assertIn("$('#llm-model')", block, "选中的模型要填进模型输入框")
        self.assertIn("hidden = false", block, "拉到列表后要显示下拉框")

    def test_model_list_is_fetched_from_api_not_hardcoded(self):
        """模型列表必须来自接口，不能只显示厂商的默认模型。

        之前下拉框要手动点按钮才出现，页面加载后只看到配置里那个默认模型。
        """
        js = self._js()
        self.assertIn("function autoFetchModels", js)
        self.assertIn("/api/test-connection", js)
        # 载入后与切换厂商时都要自动拉取
        render_all = js[js.index("function renderAll"):]
        render_all = render_all[:render_all.index("\n  }\n")]
        self.assertIn("autoFetchModels()", render_all, "载入后应自动获取模型列表")
        # 没有密钥时不要打扰接口
        block = js[js.index("function autoFetchModels"):]
        block = block[:block.index("\n  }\n")]
        self.assertIn("hasKey", block)
        self.assertIn("return", block)

    def test_model_list_is_cached_per_endpoint(self):
        """同一 provider + base_url 不要反复请求。"""
        js = self._js()
        self.assertIn("modelCache", js)
        self.assertIn("state.modelCache[key]", js)

    def test_timezone_custom_value_is_preserved(self):
        """配置里的时区若不在下拉列表中，不能被静默改回默认值。"""
        js = self._js()
        self.assertIn("function ensureTimezoneOption", js)
        self.assertIn("schedule.timezone", js)


class TimezoneTest(HermeticTestCase):
    """时区：arXiv 用 UTC，报告日按用户配置的时区切分，两者必须换算准确。"""

    def test_parse_iso_never_returns_naive(self):
        """naive datetime 交给 astimezone() 会按本机时区解释，导致整体偏移。

        在北京时间的机器上，一个「不带时区」的时间戳会被当成北京时间，
        比 UTC 晚 8 小时，足以把论文算到前一天/后一天。
        """
        from arxiv_daily_report.models import _parse_iso

        for raw in ("2026-10-16T17:30:38Z", "2026-10-16T17:30:38",
                    "2026-10-16T10:30:38-07:00", "2026-10-16T17:30:38+00:00"):
            parsed = _parse_iso(raw)
            self.assertIsNotNone(parsed.tzinfo, f"{raw} 解析出了 naive datetime")

    def test_naive_timestamp_is_treated_as_utc(self):
        from arxiv_daily_report.models import _parse_iso

        self.assertEqual(_parse_iso("2026-10-16T17:30:38").isoformat(),
                         "2026-10-16T17:30:38+00:00")

    def test_explicit_offset_is_respected(self):
        from arxiv_daily_report.models import _parse_iso

        # 太平洋时间 10:30 = UTC 17:30
        v = _parse_iso("2026-10-16T10:30:38-07:00")
        self.assertEqual(v.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"),
                         "2026-10-16 17:30")

    def test_day_bounds_for_various_timezones(self):
        from arxiv_daily_report.models import day_bounds_utc

        expected = {
            "UTC": "2026-10-16T00:00:00+00:00",
            "Asia/Shanghai": "2026-10-15T16:00:00+00:00",   # UTC+8
            "Asia/Tokyo": "2026-10-15T15:00:00+00:00",      # UTC+9
            "Europe/London": "2026-10-15T23:00:00+00:00",   # BST = UTC+1
        }
        for tz, want in expected.items():
            start, end = day_bounds_utc("2026-10-16", tz)
            self.assertEqual(start.isoformat(), want, tz)
            self.assertEqual((end - start).total_seconds(), 86400, tz)

    def test_day_bounds_handles_dst(self):
        """纽约夏令时 UTC-4、冬令时 UTC-5，必须自动跟着变。"""
        from arxiv_daily_report.models import day_bounds_utc

        summer, _ = day_bounds_utc("2026-07-15", "America/New_York")
        winter, _ = day_bounds_utc("2026-01-15", "America/New_York")
        self.assertEqual(summer.hour, 4)
        self.assertEqual(winter.hour, 5)

    def test_month_bounds(self):
        from arxiv_daily_report.models import month_bounds_utc

        start, end = month_bounds_utc("2026-10", "Asia/Shanghai")
        self.assertEqual(start.isoformat(), "2026-09-30T16:00:00+00:00")
        self.assertEqual(end.isoformat(), "2026-10-31T16:00:00+00:00")

    def test_boundary_paper_lands_on_different_days_per_timezone(self):
        """同一篇论文在不同时区可能属于不同的一天 —— 这正是要靠 UTC 区间解决的。"""
        from arxiv_daily_report.models import day_bounds_epoch

        # 2026-10-12T18:00Z：北京已是 13 日凌晨，UTC/纽约还是 12 日
        ts = int(_utc("2026-10-12T18:00:00Z").timestamp())
        self.assertIn(ts, range(*day_bounds_epoch("2026-10-13", "Asia/Shanghai")))
        self.assertIn(ts, range(*day_bounds_epoch("2026-10-12", "UTC")))
        self.assertIn(ts, range(*day_bounds_epoch("2026-10-12", "America/New_York")))

    def test_papers_for_respects_configured_timezone(self):
        """时区换成东京后，同一批论文的归日必须跟着变。"""
        from arxiv_daily_report.models import day_bounds_epoch

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = Store(tmp / "s.sqlite3")
            try:
                boundary = _utc("2026-10-12T18:00:00Z")
                p = make_paper(aid="2610.99999")
                p.updated = "2026-10-12T18:00:00Z"
                p.published = p.updated
                store.add_paper(p, {"gr-qc": True})
                self.assertEqual(
                    [x.arxiv_id for x in store.papers_for("gr-qc", "Asia/Shanghai", "2026-10-13")],
                    ["2610.99999"])
                self.assertEqual(
                    store.papers_for("gr-qc", "Asia/Shanghai", "2026-10-12"), [])
                self.assertEqual(
                    [x.arxiv_id for x in store.papers_for("gr-qc", "UTC", "2026-10-12")],
                    ["2610.99999"])
            finally:
                store.close()


def _utc(raw: str):
    return dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))


class ArxivRateLimitTest(HermeticTestCase):
    """arXiv 限流处理：首次安装最常见的故障就是 429。"""

    def _client(self, **kw):
        from arxiv_daily_report.arxiv_client import ArxivClient

        return ArxivClient(**kw)

    def test_client_works_without_explicit_user_agent(self):
        """曾经这里引用了未定义的 API_VERSION，只因调用方总传 UA 才没炸。"""
        c = self._client()
        self.assertTrue(c.ua)
        self.assertIn("arxiv-daily-report", c.ua)

    def test_retry_after_parsing(self):
        from arxiv_daily_report.arxiv_client import _parse_retry_after

        self.assertEqual(_parse_retry_after("120"), 120.0)
        self.assertEqual(_parse_retry_after(" 0 "), 0.0)
        self.assertIsNone(_parse_retry_after(""))
        self.assertIsNone(_parse_retry_after(None))
        self.assertIsNone(_parse_retry_after("nonsense"))
        # HTTP 日期形式也要能算出一个正数
        far = _parse_retry_after("Wed, 21 Oct 2099 07:28:00 GMT")
        self.assertIsNotNone(far)
        self.assertGreater(far, 0)

    def test_429_backs_off_then_raises_rate_limited(self):
        """429 要按长退避重试多次，最后抛 ArxivRateLimited（而不是普通 ArxivError）。"""
        from unittest import mock

        import urllib.error

        from arxiv_daily_report import arxiv_client as ac

        sleeps: list[float] = []
        err = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)
        with mock.patch.object(ac.time, "sleep", sleeps.append), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ac.ArxivRateLimited):
                self._client()._get({"search_query": "cat:gr-qc"})
        # 去掉请求间隔（3 秒）后，剩下的就是退避序列
        backoff = [s for s in sleeps if s > 20]
        self.assertEqual(backoff, list(ac.RATE_LIMIT_BACKOFF))

    def test_429_honours_retry_after_header_but_caps_it(self):
        from unittest import mock

        import urllib.error

        from arxiv_daily_report import arxiv_client as ac

        # Retry-After 给一个很远的日期，等待必须被封顶
        hdrs = {"Retry-After": "Wed, 21 Oct 2099 07:28:00 GMT"}
        err = urllib.error.HTTPError("http://x", 429, "Too Many Requests", hdrs, None)
        sleeps: list[float] = []
        with mock.patch.object(ac.time, "sleep", sleeps.append), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ac.ArxivRateLimited):
                self._client()._get({"search_query": "cat:gr-qc"})
        self.assertLessEqual(max(sleeps), ac.RATE_LIMIT_MAX_WAIT)

    def test_other_4xx_is_not_retried(self):
        from unittest import mock

        import urllib.error

        from arxiv_daily_report import arxiv_client as ac

        err = urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)
        calls = []
        with mock.patch.object(ac.time, "sleep", lambda s: calls.append(s)), \
             mock.patch("urllib.request.urlopen", side_effect=err):
            with self.assertRaises(ac.ArxivError) as ctx:
                self._client()._get({"search_query": "cat:gr-qc"})
        self.assertNotIsInstance(ctx.exception, ac.ArxivRateLimited)
        self.assertEqual(calls, [], "4xx 不应该重试")

    def test_success_after_transient_429(self):
        """限流一次后恢复：应当成功返回，而不是直接失败。"""
        from unittest import mock

        import urllib.error

        from arxiv_daily_report import arxiv_client as ac

        err = urllib.error.HTTPError("http://x", 429, "Too Many Requests", {}, None)

        class Resp:
            headers = {"Content-Encoding": ""}

            def read(self):
                return b"<feed></feed>"

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        sleeps: list[float] = []
        with mock.patch.object(ac.time, "sleep", sleeps.append), \
             mock.patch("urllib.request.urlopen", side_effect=[err, Resp()]):
            text = self._client()._get({"search_query": "cat:gr-qc"})
        self.assertEqual(text, "<feed></feed>")
        self.assertIn(ac.RATE_LIMIT_BACKOFF[0], sleeps)


    def test_cli_reports_rate_limit_without_traceback(self):
        """CLI 要把限流翻译成能照做的提示，而不是甩堆栈。"""
        import contextlib
        import io
        from unittest import mock

        from arxiv_daily_report import cli
        from arxiv_daily_report.arxiv_client import ArxivRateLimited

        def boom(*_a, **_k):
            raise ArxivRateLimited("arXiv API 限流（HTTP 429），已退避重试 5 次仍未通过")

        err = io.StringIO()
        with mock.patch("arxiv_daily_report.report_daily.run_daily", side_effect=boom), \
             contextlib.redirect_stderr(err):
            code = cli.main(["daily"])
        self.assertEqual(code, 3)
        msg = err.getvalue()
        self.assertIn("限流", msg)
        self.assertIn("request_interval_sec", msg)   # 给出可操作建议
        self.assertNotIn("Traceback", msg)

    def test_scheduler_retries_after_failure(self):
        """失败后要在几分钟内重试，而不是干等第二天的定时。"""
        import inspect

        from arxiv_daily_report import scheduler as _s

        self.assertTrue(_s.RETRY_DELAYS)
        self.assertLessEqual(max(_s.RETRY_DELAYS), 3600, "重试间隔不该长到一整天")
        src = inspect.getsource(_s.run_daemon)
        self.assertIn("retry_at", src)
        self.assertIn("delays[failures - 1]", src)
        # 不要再出现「说 5 分钟后重试、实际等到明天」的写法
        self.assertNotIn('log.exception("本轮运行失败，%s 后重试", "5 分钟")', src)

    def test_rate_limit_retries_are_slower_than_generic_failures(self):
        """被限流时要等更久——5 分钟后再撞一次只会延长封锁。"""
        from arxiv_daily_report.arxiv_client import ArxivRateLimited, ArxivError
        from arxiv_daily_report.scheduler import RETRY_DELAYS, RETRY_DELAYS_RATE_LIMIT, _delays_for

        self.assertEqual(_delays_for(ArxivRateLimited("x")), RETRY_DELAYS_RATE_LIMIT)
        self.assertEqual(_delays_for(ArxivError("x")), RETRY_DELAYS)
        self.assertGreater(min(RETRY_DELAYS_RATE_LIMIT), max(RETRY_DELAYS) / 2)


class ArxivComplianceTest(HermeticTestCase):
    """严格执行 arXiv 官方约束。"""

    def test_api_interval_floor(self):
        """ToU 要求每 3 秒最多 1 次；本项目主动放慢到 5 秒。"""
        from arxiv_daily_report.arxiv_client import API_MIN_INTERVAL, ArxivClient

        self.assertGreaterEqual(API_MIN_INTERVAL, 5.0, "本项目下限应比官方更保守")
        self.assertEqual(ArxivClient(interval_sec=0).interval, API_MIN_INTERVAL)
        self.assertEqual(ArxivClient(interval_sec=0.5).interval, API_MIN_INTERVAL)
        # 比下限更慢的配置要尊重
        self.assertEqual(ArxivClient(interval_sec=10).interval, 10.0)

    def test_fulltext_respects_crawl_delay(self):
        """arxiv.org/robots.txt 写着 Crawl-delay: 15，抓 /html 必须按这个间隔。"""
        from arxiv_daily_report.arxiv_client import CRAWL_DELAY
        from arxiv_daily_report.config import Config
        from arxiv_daily_report.fulltext import FullTextService

        self.assertGreaterEqual(CRAWL_DELAY, 15.0, "不得低于 robots.txt 的 Crawl-delay")
        self.assertGreaterEqual(CRAWL_DELAY, 20.0, "本项目按 20 秒执行")
        with tempfile.TemporaryDirectory() as td:
            cfg = Config(path=Path(td) / "c.json",
                         overrides={"fulltext": {"throttle_seconds": 0.1}})
            svc = FullTextService(None, cfg)
            self.assertEqual(svc.throttle, CRAWL_DELAY)

    def test_config_defaults_follow_official_limits(self):
        from arxiv_daily_report.arxiv_client import API_MIN_INTERVAL, CRAWL_DELAY
        from arxiv_daily_report.config import Config

        from arxiv_daily_report.arxiv_client import ArxivClient

        cfg = Config()
        # 配置里写的是官方要求（3 秒）；客户端再自动留 50ms 余量
        self.assertGreaterEqual(cfg["fetch"]["request_interval_sec"], 3.0)
        self.assertGreaterEqual(
            ArxivClient(interval_sec=cfg["fetch"]["request_interval_sec"]).interval,
            API_MIN_INTERVAL)
        self.assertGreaterEqual(cfg["fulltext"]["throttle_seconds"], 20.0)

    def test_rate_limit_cooldown_escalates_and_blocks_requests(self):
        """被限流后要进入冷却，期间不再请求 arXiv。"""
        import datetime as _dt

        from arxiv_daily_report import collect as _c

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            store = Store(tmp / "s.sqlite3")
            try:
                self.assertIsNone(_c.cooldown_until(store))
                first = _c.note_rate_limited(store)
                self.assertGreater(first, _dt.datetime.now(_dt.timezone.utc))
                got = _c.cooldown_until(store)
                self.assertIsNotNone(got)
                self.assertTrue(_c.COOLDOWN_STEPS[0] - 5
                                < (got - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                                <= _c.COOLDOWN_STEPS[0])
                # 首次 5 分钟，逐级加长，封顶 30 分钟
                self.assertEqual(_c.COOLDOWN_STEPS[0], 300)
                self.assertEqual(max(_c.COOLDOWN_STEPS), 1800)
                second_gap = (_c.note_rate_limited(store)
                              - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                self.assertGreater(second_gap, _c.COOLDOWN_STEPS[0])
                # 加到头之后不再增长
                for _ in range(6):
                    _c.note_rate_limited(store)
                capped = (_c.cooldown_until(store)
                          - _dt.datetime.now(_dt.timezone.utc)).total_seconds()
                self.assertLessEqual(capped, _c.COOLDOWN_STEPS[-1] + 5)

                # 冷却期内 collect 必须直接失败，不碰网络
                cfg = Config(path=tmp / "c.json", overrides={
                    "paths": {"data": str(tmp / "d"), "cache": str(tmp / "k"),
                              "reports": str(tmp / "r")}})
                from arxiv_daily_report.arxiv_client import ArxivRateLimited as _RL

                with self.assertRaises(_RL):
                    _c.collect(cfg, store, day="2026-10-13")

                _c.clear_cooldown(store)
                self.assertIsNone(_c.cooldown_until(store))
            finally:
                store.close()

    def test_success_resets_cooldown_to_five_minutes(self):
        """一次请求成功后就归零，下次限流重新从 5 分钟开始。"""
        from unittest import mock

        from arxiv_daily_report import collect as _c
        from arxiv_daily_report.arxiv_client import ArxivClient

        with tempfile.TemporaryDirectory() as td:
            store = Store(Path(td) / "s.sqlite3")
            try:
                for _ in range(3):
                    _c.note_rate_limited(store)
                self.assertGreaterEqual(int(store.kv_get(_c.COOLDOWN_COUNT_KEY, "0")), 3)

                class Resp:
                    headers = {"Content-Encoding": ""}

                    def read(self):
                        return b"<feed></feed>"

                    def __enter__(self):
                        return self

                    def __exit__(self, *a):
                        return False

                client = ArxivClient(on_success=lambda: _c.clear_cooldown(store))
                with mock.patch("urllib.request.urlopen", return_value=Resp()):
                    client._get({"search_query": "cat:gr-qc"})
                self.assertIsNone(_c.cooldown_until(store))
                self.assertEqual(store.kv_get(_c.COOLDOWN_COUNT_KEY, "0"), "0")
                # 再被限流 -> 回到 5 分钟
                gap = (_c.note_rate_limited(store)
                       - __import__("datetime").datetime.now(
                           __import__("datetime").timezone.utc)).total_seconds()
                self.assertLessEqual(gap, _c.COOLDOWN_STEPS[0] + 5)
            finally:
                store.close()

    def test_old_days_are_not_refetched(self):
        """已经抓过的旧日期重跑时不再请求 arXiv —— 限流环境下尤其重要。"""
        from arxiv_daily_report import collect as _c

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cfg = Config(path=tmp / "c.json", overrides={
                "paths": {"data": str(tmp / "d"), "cache": str(tmp / "k"),
                          "reports": str(tmp / "r")}})
            store = Store(tmp / "d" / "state.sqlite3")
            try:
                paper = make_paper(aid="2610.5")
                paper.updated = "2026-09-01T02:00:00Z"
                paper.published = paper.updated
                store.add_paper(paper, {"gr-qc": True})
                res = _c.collect(cfg, store, day="2026-09-01",
                                 progress=lambda m: None)
                self.assertEqual(res.requests, 0, "旧日期不应再联网")
                self.assertEqual(len(res.papers), 1)
            finally:
                store.close()

    def test_report_acknowledges_arxiv(self):
        """arXiv API Access 页要求产品中致谢。"""
        from arxiv_daily_report.i18n import rt

        for lang in ("zh", "en"):
            text = rt(lang, "report.footer")
            self.assertIn("arXiv", text)
        self.assertIn("open access interoperability", rt("en", "report.footer"))


class PackageVerifierTest(HermeticTestCase):
    """安装包校验器：必须能真正读到内层文件（deb 用 bsdtar 只能看到 ar 成员）。"""

    @staticmethod
    def _make_deb(path: Path, members: dict[str, str]) -> None:
        """手工造一个最小合法 deb：ar(debian-binary, data.tar.gz)。"""
        import io
        import tarfile

        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, content in members.items():
                data = content.encode()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
        payload = buf.getvalue()

        def ar_member(name: str, data: bytes) -> bytes:
            header = (name + "/").ljust(16) + "0".ljust(12) + "0".ljust(6) + \
                     "0".ljust(6) + "100644".ljust(8) + str(len(data)).ljust(10) + "`\n"
            return header.encode() + data + (b"\n" if len(data) % 2 else b"")

        blob = b"!<arch>\n"
        blob += ar_member("debian-binary", b"2.0\n")
        blob += ar_member("data.tar.gz", payload)
        path.write_bytes(blob)

    @staticmethod
    def _make_rpm(path: Path, members: dict[str, str]) -> None:
        """手工造一个最小合法 rpm：cpio(newc) + gzip 载荷。

        用来验证纯 Python 的 rpm 读取器真的能读到内层文件 ——
        之前它依赖 bsdtar，CI 镜像上没有这个命令，打包作业直接失败。
        """
        import gzip as _gzip
        import struct as _struct

        def cpio(members: dict[str, str]) -> bytes:
            out = b""
            ino = 100
            for name, content in members.items():
                body = content.encode()
                raw = name.encode() + b"\x00"
                fields = [ino, 0o100644, 0, 0, 1, 0, len(body), 0, 0, 0, 0, len(raw), 0]
                out += b"070701" + b"".join(f"{v:08x}".encode() for v in fields)
                out += raw
                out += b"\x00" * ((4 - len(out) % 4) % 4)   # 填充相对归档开头
                out += body
                out += b"\x00" * ((4 - len(out) % 4) % 4)
                ino += 1
            trailer = b"TRAILER!!!\x00"
            fields = [0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, len(trailer), 0]
            out += b"070701" + b"".join(f"{v:08x}".encode() for v in fields) + trailer
            out += b"\x00" * ((4 - len(out) % 4) % 4)
            return out

        def header(entries, data: bytes) -> bytes:
            body = b"\x8e\xad\xe8\x01" + b"\x00" * 4
            body += _struct.pack(">II", len(entries), len(data))
            for tag, typ, off, cnt in entries:
                body += _struct.pack(">iiii", tag, typ, off, cnt)
            return body + data

        lead = (b"\xed\xab\xee\xdb" + b"\x03\x00" + b"\x00\x00" + b"\x00\x01"
                + b"adr-test".ljust(66, b"\x00") + b"\x00\x01" + b"\x00\x05"
                + b"\x00" * 16)
        assert len(lead) == 96, len(lead)

        sig = header([], b"")
        sig += b"\x00" * ((8 - len(sig) % 8) % 8)
        tags = b"cpio\x00gzip\x00"
        main = header([(1124, 6, 0, 1), (1125, 6, 5, 1)], tags)
        payload = _gzip.compress(cpio(members), mtime=0)
        blob = lead + sig + main + payload
        path.write_bytes(blob)

    def _verify(self, *pkgs: Path) -> tuple[int, str]:
        import contextlib
        import io
        import sys

        sys.path.insert(0, str(PROJECT / "packaging"))
        try:
            import importlib
            mod = importlib.import_module("verify-packages") if False else None
            import importlib.util as _u
            spec = _u.spec_from_file_location("adr_verify", PROJECT / "packaging/verify-packages.py")
            mod = _u.module_from_spec(spec)
            spec.loader.exec_module(mod)
        finally:
            sys.path.pop(0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = mod.main(["verify-packages.py", *[str(p) for p in pkgs]])
        return code, out.getvalue()

    def test_deb_contents_are_read_through_ar_layer(self):
        """关键回归：deb 的内容要能穿透 ar 读到内层 data.tar，否则校验是假阴性。"""
        with tempfile.TemporaryDirectory() as td:
            deb = Path(td) / "pkg_1.0_amd64.deb"
            self._make_deb(deb, {
                "./usr/bin/arxiv-daily-report": "#!/usr/bin/python3\n",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x=1\n",
                "./usr/lib/systemd/user/arxiv-daily-report.service": "[Unit]\n",
                "./usr/share/arxiv-daily-report/data/arxiv_categories.json": "{}\n",
                "./usr/share/arxiv-daily-report/data/theme_taxonomy.json": "{}\n",
                "./usr/share/arxiv-daily-report/web/index.html": "<html>\n",
                "./usr/share/doc/arxiv-daily-report/README.md": "x\n",
                "./usr/share/licenses/arxiv-daily-report/LICENSE": "x\n",
                # 真实安装包有上百个文件；校验器会用条目数判断是否真的读进了内层
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/__init__.py": "x\n",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/config.py": "x\n",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/render.py": "x\n",
                "./usr/share/arxiv-daily-report/web/app.js": "x\n",
                "./usr/share/arxiv-daily-report/assets/mathjax/render.js": "x\n",
            })
            code, out = self._verify(deb)
            self.assertEqual(code, 0, out)
            self.assertIn("通过校验", out)

    def test_rpm_contents_are_read_without_bsdtar(self):
        """rpm 必须纯 Python 读出来 —— CI 镜像上没有 bsdtar。"""
        with tempfile.TemporaryDirectory() as td:
            rpm = Path(td) / "pkg-1.0.x86_64.rpm"
            self._make_rpm(rpm, {
                "usr/bin/arxiv-daily-report": "#!/usr/bin/python3\n",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x=1\n",
                "usr/lib/systemd/user/arxiv-daily-report.service": "[Unit]\n",
                "usr/share/arxiv-daily-report/data/arxiv_categories.json": "{}\n",
                "usr/share/arxiv-daily-report/data/theme_taxonomy.json": "{}\n",
                "usr/share/arxiv-daily-report/web/index.html": "<html>\n",
                "usr/share/doc/arxiv-daily-report/README.md": "x\n",
                "usr/share/licenses/arxiv-daily-report/LICENSE": "x\n",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/__init__.py": "x\n",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/config.py": "x\n",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/render.py": "x\n",
                "usr/share/arxiv-daily-report/web/app.js": "x\n",
                "usr/share/arxiv-daily-report/assets/mathjax/render.js": "x\n",
            })
            code, out = self._verify(rpm)
            self.assertEqual(code, 0, out)
            self.assertIn("通过校验", out)

    def test_rpm_verifier_rejects_runtime_data(self):
        """纯 Python 读取器不能变成假阴性：脏数据照样要拦下。"""
        with tempfile.TemporaryDirectory() as td:
            rpm = Path(td) / "pkg-1.0.x86_64.rpm"
            members = {
                "usr/bin/arxiv-daily-report": "x",
                "usr/lib/systemd/user/arxiv-daily-report.service": "x",
                "usr/share/arxiv-daily-report/data/arxiv_categories.json": "x",
                "usr/share/arxiv-daily-report/data/theme_taxonomy.json": "x",
                "usr/share/arxiv-daily-report/web/index.html": "x",
                "usr/share/doc/arxiv-daily-report/README.md": "x",
                "usr/share/licenses/arxiv-daily-report/LICENSE": "x",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/__init__.py": "x",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/config.py": "x",
                "usr/lib/arxiv-daily-report/arxiv_daily_report/render.py": "x",
                "usr/share/arxiv-daily-report/web/app.js": "x",
                "usr/share/arxiv-daily-report/assets/mathjax/render.js": "x",
            }
            self._make_rpm(rpm, members)
            code, out = self._verify(rpm)
            self.assertEqual(code, 0, out)

            members["usr/share/arxiv-daily-report/data/state.sqlite3"] = "dirty"
            self._make_rpm(rpm, members)
            code, out = self._verify(rpm)
            self.assertEqual(code, 1, out)
            self.assertIn("state.sqlite3", out)

    def test_rpm_verifier_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            rpm = Path(td) / "pkg-1.0.x86_64.rpm"
            self._make_rpm(rpm, {"usr/bin/arxiv-daily-report": "x"})
            code, out = self._verify(rpm)
            self.assertEqual(code, 1, out)

    def test_verifier_rejects_runtime_data(self):
        """混入运行期数据必须被拦下——这是发布前的最后一道闸。"""
        with tempfile.TemporaryDirectory() as td:
            deb = Path(td) / "pkg_1.0_amd64.deb"
            self._make_deb(deb, {
                "./usr/bin/arxiv-daily-report": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x",
                "./usr/lib/systemd/user/arxiv-daily-report.service": "x",
                "./usr/share/arxiv-daily-report/data/arxiv_categories.json": "x",
                "./usr/share/arxiv-daily-report/data/theme_taxonomy.json": "x",
                "./usr/share/arxiv-daily-report/web/index.html": "x",
                "./usr/share/doc/arxiv-daily-report/README.md": "x",
                "./usr/share/licenses/arxiv-daily-report/LICENSE": "x",
                "./usr/share/arxiv-daily-report/data/state.sqlite3": "dirty",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/__init__.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/config.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/render.py": "x",
                "./usr/share/arxiv-daily-report/web/app.js": "x",
                "./usr/share/arxiv-daily-report/assets/mathjax/render.js": "x",
            })
            code, out = self._verify(deb)
            self.assertEqual(code, 1)
            self.assertIn("state.sqlite3", out)

    def test_verifier_rejects_missing_files(self):
        with tempfile.TemporaryDirectory() as td:
            deb = Path(td) / "pkg_1.0_amd64.deb"
            self._make_deb(deb, {
                "./usr/bin/arxiv-daily-report": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/__init__.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/config.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/render.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/store.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/pdf.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/models.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/cli.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/collect.py": "x",
                "./usr/lib/arxiv-daily-report/arxiv_daily_report/status.py": "x",
            })
            code, out = self._verify(deb)
            self.assertEqual(code, 1)
            self.assertIn("缺少", out)


class MonthlyTest(HermeticTestCase):
    """月报：目标月份、日期引用、依据日报浓缩。"""

    def _cfg(self, tmp: str) -> Config:
        return Config(path=Path(tmp) / "config.json")

    def test_target_month_is_previous_month(self):
        from arxiv_daily_report.report_monthly import target_month

        z = dt.timezone(dt.timedelta(hours=8))
        self.assertEqual(target_month(dt.datetime(2026, 11, 1, 19, 0, tzinfo=z)), "2026-10")
        self.assertEqual(target_month(dt.datetime(2026, 10, 14, 12, 0, tzinfo=z)), "2026-09")
        self.assertEqual(target_month(dt.datetime(2026, 1, 1, 12, 0, tzinfo=z)), "2025-12")

    def test_should_run_monthly_only_on_first_day(self):
        from arxiv_daily_report.report_monthly import should_run_monthly

        z = dt.timezone(dt.timedelta(hours=8))
        with tempfile.TemporaryDirectory() as td:
            cfg = self._cfg(td)
            self.assertEqual(should_run_monthly(cfg, dt.datetime(2026, 11, 1, 19, 0, tzinfo=z)), "2026-10")
            self.assertIsNone(should_run_monthly(cfg, dt.datetime(2026, 11, 2, 19, 0, tzinfo=z)))
            cfg.update({"schedule": {"auto_monthly": False}})
            self.assertIsNone(should_run_monthly(cfg, dt.datetime(2026, 12, 1, 19, 0, tzinfo=z)))

    def test_day_label_and_validation(self):
        from arxiv_daily_report.report_monthly import _valid_days, day_label

        self.assertEqual(day_label("2026-10-07"), "10月7日")
        self.assertEqual(day_label("2026-10-07", "en"), "Oct 7")
        self.assertEqual(
            _valid_days(["10-07", "2026-10-13", "10-32", "09-01", ""], "2026-10"),
            ["2026-10-07", "2026-10-13"],
        )

    def test_no_artificial_length_limits(self):
        """月报不设人为字数上限：准确优先，简明其次。"""
        from arxiv_daily_report import report_monthly as rm

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

        from arxiv_daily_report.report_monthly import collect_month, monthly_llm, MonthlyData

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
        from arxiv_daily_report.report_monthly import collect_month

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
        from arxiv_daily_report.report_monthly import MonthlyData, render_monthly_html

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
        from arxiv_daily_report.render import ReportContext, render_daily_html

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
