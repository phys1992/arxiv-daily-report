"""配置与密钥管理。

设计约束（可迁移目录）：
  * 所有默认路径都相对项目根目录，目录整体移动/改名不影响运行；
  * 可用环境变量 GRQC_HOME 把全部可变状态（配置、密钥、数据、报告）重定位到别处；
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
# 项目根目录：grqc/config.py -> grqc/ -> 项目根
# ---------------------------------------------------------------------------
PACKAGE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_DIR.parent

APP_NAME = "grqc-daily"


def home_dir() -> Path:
    """可变状态（配置/密钥/数据/报告）的根目录。

    优先级：
      1. GRQC_HOME 环境变量；
      2. 源码检出（git clone / 解压 zip / pip install -e）→ 用项目根，
         这样「整个目录拷走就能用」，符合可迁移的设计目标；
      3. pip 安装进 site-packages 等场景 → 用用户数据目录（XDG）。

    第 3 条很重要：site-packages 通常可写，但把用户数据写进去会在
    升级/卸载时被清掉，也违反 Python 打包惯例。
    """
    env = os.environ.get("GRQC_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    if _is_source_checkout():
        return PROJECT_ROOT
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share"
    return (base / APP_NAME).resolve()


def _is_source_checkout() -> bool:
    """判断当前是不是从源码目录运行的（wheel 里不含 run.py）。"""
    if (PROJECT_ROOT / "run.py").exists():
        return True
    if (PROJECT_ROOT / ".git").exists():
        return True
    return (PROJECT_ROOT / "assets" / "fonts").is_dir()


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
    # ---- 路径（相对项目根或 GRQC_HOME；绝对路径原样使用）----
    "paths": {
        "reports": "reports",
        "data": "data",
        "publish_dir": "",             # 例如 Seafile 同步目录；空则不发布
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
    "report": {
        "font_profile": "source-han",  # source-han | system | custom
        "font_file_regular": "",
        "font_file_bold": "",
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
        "host": "127.0.0.1",
        "port": 8765,
        "password": "",                # 绑非回环地址时必填
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
      GRQC_HOME            可变状态根目录（在 home_dir() 中处理）
      GRQC_WEB_PASSWORD    Web 访问密码（绑定非回环地址时必需）
      GRQC_WEB_HOST / GRQC_WEB_PORT
      GRQC_PROVIDER / GRQC_MODEL / GRQC_BASE_URL
      GRQC_REPORT_LANGUAGE / GRQC_UI_LANGUAGE
    """
    out: dict[str, Any] = {}
    web: dict[str, Any] = {}
    if os.environ.get("GRQC_WEB_PASSWORD"):
        web["password"] = os.environ["GRQC_WEB_PASSWORD"]
    if os.environ.get("GRQC_WEB_HOST"):
        web["host"] = os.environ["GRQC_WEB_HOST"]
    if os.environ.get("GRQC_WEB_PORT"):
        try:
            web["port"] = int(os.environ["GRQC_WEB_PORT"])
        except ValueError:
            pass
    if web:
        out["web"] = web
    llm: dict[str, Any] = {}
    for env, field in (("GRQC_PROVIDER", "provider"), ("GRQC_MODEL", "model"),
                       ("GRQC_BASE_URL", "base_url")):
        if os.environ.get(env):
            llm[field] = os.environ[env]
    if llm:
        out["llm"] = llm
    for env, field in (("GRQC_REPORT_LANGUAGE", "report_language"),
                       ("GRQC_UI_LANGUAGE", "ui_language")):
        if os.environ.get(env):
            out[field] = os.environ[env]
    return out


class Config:
    """配置对象。读写 JSON，原子落盘，密钥分离存储。"""

    def __init__(self, path: Path | None = None, overrides: dict | None = None):
        self.home = home_dir()
        self.path = Path(path).expanduser() if path else self.home / "config.json"
        self.secrets_path = self.path.with_name("secrets.json")
        self._data: dict[str, Any] = {}
        self._secrets: dict[str, Any] = {}
        self.load()
        if overrides:
            self._data = _deep_merge(self._data, overrides)

    # -- 载入 / 保存 --------------------------------------------------------
    def ensure_home(self) -> None:
        """确保状态目录存在且可写，给出可操作的报错而不是原始 traceback。"""
        try:
            self.home.mkdir(parents=True, exist_ok=True)
            probe = self.home / ".write-test"
            probe.touch()
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                f"状态目录不可写：{self.home}\n"
                f"  原因：{exc}\n"
                "  解决：设置环境变量 GRQC_HOME 指向一个可写目录，例如：\n"
                "        export GRQC_HOME=" + "$HOME/grqc-daily-data"
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
        return self.resolve(self._data["paths"]["reports"])

    @property
    def data_dir(self) -> Path:
        return self.resolve(self._data["paths"]["data"])

    @property
    def publish_dir(self) -> Path | None:
        v = (self._data["paths"].get("publish_dir") or "").strip()
        return self.resolve(v) if v else None

    def data_asset(self, name: str) -> Path:
        """随仓库分发的数据文件（分类表、主题表等）。

        优先 GRQC_HOME/data，其次项目内的 data/。
        这样即使把 GRQC_HOME 指到项目外（部署/容器常见做法），
        随仓库带来的数据资产也仍然可用。
        """
        p = self.data_dir / name
        if p.exists():
            return p
        return PROJECT_ROOT / "data" / name

    @property
    def assets_dir(self) -> Path:
        """随仓库分发的静态资产（字体、MathJax）永远在项目内，不随 GRQC_HOME 移动。

        允许用 GRQC_ASSETS 覆盖，方便把字体放到别处（例如容器里挂载）。
        """
        env = os.environ.get("GRQC_ASSETS", "").strip()
        if env:
            return Path(env).expanduser().resolve()
        return PROJECT_ROOT / "assets"

    @property
    def web_dir(self) -> Path:
        return PROJECT_ROOT / "web"

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
