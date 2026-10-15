"""配置与密钥管理。

设计约束（可迁移目录）：
  * 所有默认路径都相对项目根目录，目录整体移动/改名不影响运行；
  * 可用环境变量 ADR_HOME 把全部可变状态（配置、密钥、数据、报告）重定位到别处；
  * config.json / secrets.json 不入库（见 .gitignore），仓库只带 config.example.json。
"""
from __future__ import annotations

import copy
import json
import os
import secrets as _secrets
import stat
import tempfile
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# 项目根目录：arxiv_daily_report/config.py -> arxiv_daily_report/ -> 项目根
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

APP_NAME = "arxiv-daily-report"


def _xdg(env_var: str, default_rel: str) -> Path:
    v = os.environ.get(env_var, "").strip()
    base = Path(v).expanduser() if v else Path.home() / default_rel
    return (base / APP_NAME).resolve()


def _resolve_dir(env_single: str, xdg_var: str, xdg_default: str,
                 portable_sub: str | None) -> Path:
    """目录解析优先级：

      1. ADR_HOME              —— 全部状态放进一个目录（便携部署）
      2. ADR_CONFIG_HOME 等    —— 单项覆盖
      3. 源码检出              —— 用项目根，整个目录拷走就能用
      4. XDG 默认              —— 发行版包/pip 安装的标准位置
    """
    home = os.environ.get("ADR_HOME", "").strip()
    if home:
        root = Path(home).expanduser().resolve()
        return root if portable_sub is None else (root / portable_sub)
    single = os.environ.get(env_single, "").strip()
    if single:
        return Path(single).expanduser().resolve()
    if _is_source_checkout():
        return PROJECT_ROOT if portable_sub is None else (PROJECT_ROOT / portable_sub)
    return _xdg(xdg_var, xdg_default)


def portable_root() -> Path | None:
    """便携模式根目录；不是便携模式则返回 None（此时走 XDG 目录）。"""
    home = os.environ.get("ADR_HOME", "").strip()
    if home:
        return Path(home).expanduser().resolve()
    if _is_source_checkout():
        return PROJECT_ROOT
    return None


def config_dir() -> Path:
    """配置目录：<ADR_HOME> | ADR_CONFIG_HOME | 项目根 | ~/.config/arxiv-daily-report"""
    return _resolve_dir("ADR_CONFIG_HOME", "XDG_CONFIG_HOME", ".config", None)


def data_dir() -> Path:
    """数据目录（库、报告、原始 XML）：…/data 或 ~/.local/share/arxiv-daily-report"""
    return _resolve_dir("ADR_DATA_HOME", "XDG_DATA_HOME", ".local/share", "data")


def cache_dir() -> Path:
    """缓存目录（可安全删除）：…/cache 或 ~/.cache/arxiv-daily-report"""
    return _resolve_dir("ADR_CACHE_HOME", "XDG_CACHE_HOME", ".cache", "cache")


def home_dir() -> Path:
    """兼容旧调用：返回数据目录。新代码请直接用 config_dir/data_dir/cache_dir。"""
    return data_dir()


# 发行版包安装布局：Python 包与数据文件按 FHS 放置。
# 前缀可用 ADR_PREFIX 覆盖（默认 /usr），便于 /usr/local、Nix 等非标准布局，
# 也方便测试时把安装树解到临时目录验证。
def system_prefix() -> Path:
    return Path(os.environ.get("ADR_PREFIX", "/usr")).expanduser().resolve()


def system_lib_dir() -> Path:
    """Python 包所在目录，例如 /usr/lib/arxiv-daily-report。"""
    return system_prefix() / "lib" / APP_NAME


def system_data_dir() -> Path:
    """数据文件目录，例如 /usr/share/arxiv-daily-report。"""
    return system_prefix() / "share" / APP_NAME


def _is_source_checkout() -> bool:
    """判断当前是不是从源码目录运行的。

    只用 run.py / .git 判断：wheel 里也会带 data/ 目录，
    不能拿数据文件是否存在当依据。
    """
    return (PROJECT_ROOT / "run.py").exists() or (PROJECT_ROOT / ".git").exists()


# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------
DEFAULT_CATEGORY = "gr-qc"

DEFAULT_CONFIG: dict[str, Any] = {
    "version": 1,
    # ---- 界面 / 报告语言 ----
    "ui_language": "zh-CN",            # zh-CN | en
    "report_language": "zh",           # zh | en | zh-en
    # ---- 分类订阅 ----
    "subscriptions": [
        {
            "category": DEFAULT_CATEGORY,
            "enabled": True,
            "report_name": "",          # 空则用分类官方全称
            "out_dir": "",              # 空则用 reports/<category>
        }
    ],
    "output_mode": "per_category",     # per_category | combined | both
    "crosslist_policy": "primary",     # primary | all | combined_dedup
    "combined_out_dir": "",            # 空则用 reports/combined
    # ---- 调度 ----
    "schedule": {
        "mode": "daily",               # daily | interval | manual
        "time": "19:00",               # 每天运行时刻（schedule.timezone 时区）
        "interval_minutes": 1440,
        "timezone": "Asia/Shanghai",
        "report_day_offset": 1,        # 报告日 = 运行日 - N 天
        "auto_monthly": True,          # 每月 1 号自动生成上一个自然月的简报
        "catch_up_days": 7,            # 每次回溯天数（周末/节假日补录）
        "retry": 3,
    },
    # ---- 路径（相对项目根或 ADR_HOME；绝对路径原样使用）----
    "paths": {
        # 留空表示自动推导：
        #   便携模式（ADR_HOME / 源码检出）-> <home>/reports、<home>/data
        #   安装形态                       -> <数据目录>/reports
        "reports": "",
        "data": "",
        "cache": "",                   # 留空 -> 便携 <home>/cache；安装 ~/.cache/arxiv-daily-report
        "publish_dir": "",             # 例如同步盘目录；空则不发布
    },
    # ---- 抓取 ----
    "fetch": {
        "page_size": 100,
        "request_interval_sec": 3.0,   # arXiv ToU：每 3 秒最多 1 次请求
        "timeout_sec": 60,
        "user_agent": "",              # 空则按 contact 自动生成
        "contact": "",                 # 建议填写邮箱或仓库地址，便于 arXiv 联系你
        "max_pages": 60,
    },
    # ---- 大模型 ----
    "llm": {
        "provider": "deepseek",        # deepseek | moonshot | openai | anthropic | custom
        "base_url": "",                # 空则用厂商预设
        "model": "",                   # 空则用厂商预设
        "temperature": 0,
        "max_tokens": 8192,
        "timeout_sec": 240,
        "concurrency": 4,
        "disable_thinking": True,      # DeepSeek 等厂商：结构化摘要关闭思考模式
        "fallback_provider": "",       # 主模型失败时降级；空则不降级
        "fallback_model": "",
        "save_keys": True,             # False = 只从环境变量读取密钥
    },
    # ---- 报告内容 ----
    # ---- 全文（读取论文结论/讨论小节）----
    "fulltext": {
        "enabled": True,               # 日报摘要是否结合论文全文的结论/讨论部分
        "source": "auto",              # auto | arxiv-html | ar5iv
        "max_chars": 4000,             # 每篇送入模型的结论原文上限（字符）
        "throttle_seconds": 1.0,       # 抓取间隔，别给 arXiv 添压力
        "timeout_seconds": 30,
    },
    "report": {
        # 字体由系统提供（不再随仓库分发）。
        # font_family 留空则用内置默认栈：西文字体在前、中文字体在后；
        # 逐字符回退 => 英文走西文字体、汉字走中文字体（优先思源黑体）。
        "font_family": "",
        "font_file": "",               # 可选：额外的字体文件，会内联并置于栈首
        "math_backend": "auto",        # auto | mathjax | unicode
        "include_daily_highlights": True,
        "include_abstract_note": True,
        "author_limit": 3,             # 参考文献中作者超过该数则 et al.
        "themes": [],                  # 空则用 data/theme_taxonomy.json
        "keep_raw_xml": True,
        "keep_html": False,
    },
    # ---- Web 界面 ----
    "web": {
        "enabled": True,               # daemon 常驻时是否同时提供 Web 配置界面
        "host": "127.0.0.1",
        "port": 8765,
        "password": "",                # 绑非回环地址时必填
        # 默认 false：只监听回环地址时信任本机，打开 http://127.0.0.1:8765 即可用，
        # 不需要任何令牌或密码（与 v2rayA 等本地工具一致）。
        # 设为 true 则恢复「必须用带 ?token= 的地址访问」。
        "require_token": False,
        "open_browser": True,
    },
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _env_overrides() -> dict[str, Any]:
    """环境变量覆盖（便于容器/CI 部署，避免把密钥或密码写进文件）。

    支持：
      ADR_HOME            可变状态根目录（在 home_dir() 中处理）
      ADR_WEB_PASSWORD    Web 访问密码（绑定非回环地址时必需）
      ADR_WEB_HOST / ADR_WEB_PORT
      ADR_PROVIDER / ADR_MODEL / ADR_BASE_URL
      ADR_REPORT_LANGUAGE / ADR_UI_LANGUAGE
    """
    out: dict[str, Any] = {}
    web: dict[str, Any] = {}
    if os.environ.get("ADR_WEB_PASSWORD"):
        web["password"] = os.environ["ADR_WEB_PASSWORD"]
    if os.environ.get("ADR_WEB_HOST"):
        web["host"] = os.environ["ADR_WEB_HOST"]
    if os.environ.get("ADR_WEB_PORT"):
        try:
            web["port"] = int(os.environ["ADR_WEB_PORT"])
        except ValueError:
            pass
    if web:
        out["web"] = web
    llm: dict[str, Any] = {}
    for env, field in (("ADR_PROVIDER", "provider"), ("ADR_MODEL", "model"),
                       ("ADR_BASE_URL", "base_url")):
        if os.environ.get(env):
            llm[field] = os.environ[env]
    if llm:
        out["llm"] = llm
    for env, field in (("ADR_REPORT_LANGUAGE", "report_language"),
                       ("ADR_UI_LANGUAGE", "ui_language")):
        if os.environ.get(env):
            out[field] = os.environ[env]
    return out


class Config:
    """配置对象。读写 JSON，原子落盘，密钥分离存储。"""

    def __init__(self, path: Path | None = None, overrides: dict | None = None):
        self.portable = portable_root()
        self.config_home = config_dir()
        self.state_home = data_dir()
        self.cache_home = cache_dir()
        # 便携模式下 home 就是项目根/ADR_HOME，便于 compat 与状态展示
        self.home = self.portable if self.portable is not None else self.state_home
        self.path = Path(path).expanduser() if path else self.config_home / "config.json"
        self.secrets_path = self.path.with_name("secrets.json")
        self._data: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self.load()
        if overrides:
            self._data = _deep_merge(self._data, overrides)

    # -- 载入 / 保存 --------------------------------------------------------
    def ensure_home(self) -> None:
        """确保配置/数据/缓存三个目录存在且可写，给出可操作的报错。"""
        for label, d in (("配置", self.config_home), ("数据", self.state_home),
                         ("缓存", self.cache_home)):
            try:
                d.mkdir(parents=True, exist_ok=True)
                probe = d / ".write-test"
                probe.touch()
                probe.unlink()
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(
                    f"{label}目录不可写：{d}\n"
                    f"  原因：{exc}\n"
                    "  解决：用环境变量把目录重定向到可写位置，例如\n"
                    "        export ADR_HOME=" + "$HOME/arxiv-daily-report-data\n"
                    "        （也可分别设置 ADR_CONFIG_HOME / ADR_DATA_HOME / ADR_CACHE_HOME）"
                ) from exc

    def load(self) -> "Config":
        self.ensure_home()
        raw: dict[str, Any] = {}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception as exc:  # 配置损坏时不静默吞掉
                raise RuntimeError(f"配置文件解析失败 {self.path}: {exc}") from exc
        self._data = _deep_merge(DEFAULT_CONFIG, raw)
        # 环境变量优先级高于文件，便于容器部署
        env_patch = _env_overrides()
        if env_patch:
            self._data = _deep_merge(self._data, env_patch)
        if self.secrets_path.exists():
            try:
                self._secrets = json.loads(self.secrets_path.read_text(encoding="utf-8"))
            except Exception:
                self._secrets = {}
        return self

    def save(self) -> None:
        _atomic_write_json(self.path, self._data, mode=0o644)
        if self._data["llm"].get("save_keys", True) and self._secrets:
            _atomic_write_json(self.secrets_path, self._secrets, mode=0o600)

    # -- 访问 --------------------------------------------------------------
    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def update(self, patch: dict) -> None:
        self._data = _deep_merge(self._data, patch)

    # -- 路径 --------------------------------------------------------------
    def resolve(self, value: str | Path, *, base: Path | None = None) -> Path:
        """相对路径按项目根解析；绝对路径原样返回；展开 ~。"""
        p = Path(str(value)).expanduser()
        if p.is_absolute():
            return p
        return ((base or self.home) / p).resolve()

    @property
    def reports_dir(self) -> Path:
        """报告输出目录。paths.reports 留空时：
        便携模式 <root>/reports；安装模式 <数据目录>/reports。"""
        v = (self._data["paths"].get("reports") or "").strip()
        if v:
            return self.resolve(v)
        if self.portable is not None:
            return (self.portable / "reports").resolve()
        return (self.state_home / "reports").resolve()

    @property
    def data_dir(self) -> Path:
        """库、原始 XML 等数据目录（paths.data 可覆盖）。"""
        v = (self._data["paths"].get("data") or "").strip()
        if v:
            return self.resolve(v)
        return self.state_home

    @property
    def cache_dir(self) -> Path:
        """缓存目录（可安全删除）。"""
        v = (self._data["paths"].get("cache") or "").strip()
        if v:
            return self.resolve(v)
        return self.cache_home

    @property
    def publish_dir(self) -> Path | None:
        v = (self._data["paths"].get("publish_dir") or "").strip()
        return self.resolve(v) if v else None

    @property
    def web_dir(self) -> Path:
        """Web 前端静态文件目录：项目内 web/（源码）或 /usr/share/.../web（安装包）。"""
        local = PROJECT_ROOT / "web"
        if local.is_dir():
            return local
        return system_data_dir() / "web"

    def data_asset(self, name: str) -> Path:
        """随仓库分发的数据文件（分类表、主题表等）。

        优先 ADR_HOME/data，其次项目内的 data/。
        这样即使把 ADR_HOME 指到项目外（部署/容器常见做法），
        随仓库带来的数据资产也仍然可用。
        """
        for cand in (self.data_dir / name,
                     PROJECT_ROOT / "data" / name,
                     system_data_dir() / "data" / name):
            if cand.exists():
                return cand
        return PROJECT_ROOT / "data" / name

    @property
    def assets_dir(self) -> Path:
        """静态资产目录（MathJax 等）。搜索顺序：
        ADR_ASSETS → 项目内 assets/ → /usr/share/arxiv-daily-report/assets。
        """
        env = os.environ.get("ADR_ASSETS", "").strip()
        if env:
            return Path(env).expanduser().resolve()
        local = PROJECT_ROOT / "assets"
        if local.is_dir():
            return local
        return system_data_dir() / "assets"

    # -- 密钥 --------------------------------------------------------------
    # 环境变量优先级最高，其次 secrets.json
    _ENV_KEYS = {
        "deepseek": "DEEPSEEK_API_KEY",
        "moonshot": "MOONSHOT_API_KEY",
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "custom": "CUSTOM_API_KEY",
    }

    def api_key(self, provider: str | None = None) -> str:
        provider = provider or self._data["llm"]["provider"]
        env = self._ENV_KEYS.get(provider)
        if env and os.environ.get(env):
            return os.environ[env].strip()
        if provider == "custom" and os.environ.get("OPENAI_API_KEY"):
            # 本地 OpenAI 兼容服务常直接复用 OPENAI_API_KEY
            return os.environ["OPENAI_API_KEY"].strip()
        return (self._secrets.get("api_keys", {}) or {}).get(provider, "") or ""

    def set_api_key(self, provider: str, key: str) -> None:
        keys = self._secrets.setdefault("api_keys", {})
        if key:
            keys[provider] = key
        else:
            keys.pop(provider, None)

    def mask_key(self, provider: str) -> str:
        k = self.api_key(provider)
        if not k:
            return ""
        if len(k) <= 10:
            return "•" * len(k)
        return f"{k[:6]}{'•' * 8}{k[-4:]}"

    def key_source(self, provider: str) -> str:
        env = self._ENV_KEYS.get(provider)
        if env and os.environ.get(env):
            return f"env:{env}"
        if (self._secrets.get("api_keys", {}) or {}).get(provider):
            return "secrets.json"
        return ""

    # -- Web 会话令牌 -------------------------------------------------------
    def session_token(self) -> str:
        tok = self._secrets.get("session_token")
        if not tok:
            tok = _secrets.token_urlsafe(24)
            self._secrets["session_token"] = tok
            self.save()
        return tok


def _atomic_write_json(path: Path, data: dict, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
