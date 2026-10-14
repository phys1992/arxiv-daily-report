"""Web 配置界面后端（标准库 http.server，零额外依赖）。

安全：
  * 默认只监听 127.0.0.1；绑到其它地址时必须设置密码；
  * 启动生成一次性会话令牌，写进 URL，所有 /api/* 请求校验；
  * API Key 只回显掩码，永不返回明文。
"""
from __future__ import annotations

import hmac
import html
import json
import logging
import mimetypes
import re
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from .config import Config
from .i18n import UI, t
from .llm.client import LLMClient
from .llm.registry import describe_all
from .store import Store

log = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 只读元数据
# ---------------------------------------------------------------------------
def load_categories(cfg: Config) -> dict[str, str]:
    p = cfg.data_asset("arxiv_categories.json")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"gr-qc": "General Relativity and Quantum Cosmology"}


def load_themes(cfg: Config) -> list[dict]:
    p = cfg.data_asset("theme_taxonomy.json")
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("themes", [])
    except Exception:  # noqa: BLE001
        return []


def category_groups(cats: dict[str, str]) -> list[dict]:
    """按归档分组，便于前端渲染树。"""
    groups: dict[str, list[dict]] = {}
    for code, name in cats.items():
        arch = code.split(".")[0]
        groups.setdefault(arch, []).append({"code": code, "name": name})
    order = ["gr-qc", "hep-th", "hep-ph", "hep-ex", "hep-lat", "quant-ph", "math-ph",
             "nucl-th", "nucl-ex", "astro-ph", "cond-mat", "physics", "math", "cs",
             "stat", "nlin", "eess", "econ", "q-bio", "q-fin"]
    out: list[dict] = []
    for arch in order:
        if arch in groups:
            out.append({"archive": arch, "items": sorted(groups.pop(arch), key=lambda x: x["code"])})
    for arch in sorted(groups):
        out.append({"archive": arch, "items": sorted(groups[arch], key=lambda x: x["code"])})
    return out


# ---------------------------------------------------------------------------
# 运行状态（后台任务）
# ---------------------------------------------------------------------------
class JobRunner:
    """单后台任务执行器：同一时刻只跑一个生成任务，状态可轮询。"""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state: dict[str, Any] = {"running": False, "lines": [], "started": 0.0,
                                      "finished": 0.0, "ok": None, "kind": ""}

    def busy(self) -> bool:
        return bool(self.state.get("running"))

    def start(self, kind: str, fn) -> bool:
        with self.lock:
            if self.state.get("running"):
                return False
            self.state = {"running": True, "lines": [], "started": time.time(),
                          "finished": 0.0, "ok": None, "kind": kind}

        def progress(msg: str) -> None:
            with self.lock:
                self.state["lines"].append(str(msg))
                if len(self.state["lines"]) > 400:
                    del self.state["lines"][:200]
            log.info("%s", msg)

        def run() -> None:
            try:
                fn(progress)
                ok, err = True, ""
            except Exception as exc:  # noqa: BLE001
                ok, err = False, f"{type(exc).__name__}: {exc}"
                log.exception("后台任务失败")
            with self.lock:
                self.state.update(running=False, ok=ok, finished=time.time(),
                                  error=err)

        threading.Thread(target=run, daemon=True).start()
        return True

    def snapshot(self) -> dict:
        with self.lock:
            s = dict(self.state)
            s["lines"] = list(self.state["lines"])[-120:]
            return s


JOBS = JobRunner()


# ---------------------------------------------------------------------------
# 请求处理
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "grqc-daily"
    cfg: Config
    store: Store
    token: str

    # -- 工具 ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _authed(self) -> bool:
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        if params.get("token", [""])[0] == self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"grqc_token=([^;]+)", cookie)
        if m and m.group(1) == self.token:
            return True
        header = self.headers.get("X-Auth-Token", "")
        return bool(header) and header == self.token

    def _json(self, obj: Any, code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:  # noqa: BLE001
            return {}

    def _static(self, rel: str) -> None:
        base = PROJECT_ROOT / "web"
        path = (base / rel.lstrip("/")).resolve()
        if not str(path).startswith(str(base)) or not path.is_file():
            self.send_error(404, "not found")
            return
        ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype + ("; charset=utf-8" if ctype.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _report_file(self, rel: str) -> None:
        root = self.cfg.reports_dir
        path = (root / rel.lstrip("/")).resolve()
        if not str(path).startswith(str(root)) or not path.is_file():
            self.send_error(404, "not found")
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Disposition", f'inline; filename="{path.name}"')
        self.end_headers()
        self.wfile.write(data)

    # -- 路由 ------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            params = urllib.parse.parse_qs(u.query)
            token = params.get("token", [""])[0]
            if token and token == self.token:
                self.send_response(302)
                self.send_header("Set-Cookie", f"grqc_token={token}; Path=/; SameSite=Strict")
                self.send_header("Location", "/index.html")
                self.end_headers()
                return
            if not self._authed():
                # 设了密码就给出登录页，否则提示用启动时打印的带令牌地址
                if self._password():
                    self._serve_login()
                else:
                    self.send_error(403, "需要访问令牌：请使用启动时打印的带 ?token=... 的地址")
                return
            self._static("index.html")
            return
        if path == "/login":
            self._serve_login()
            return
        if path.startswith("/reports/"):
            self._report_file(path[len("/reports/"):])
            return
        if path.startswith("/api/"):
            if not self._authed():
                self._json({"error": "unauthorized", "need_login": bool(self._password())}, 401)
                return
            self._api_get(path[len("/api/"):])
            return
        self._static(path)

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/api/"):
            self._json({"error": "not found"}, 404)
            return
        route = self.path[len("/api/"):]
        if route == "login":                       # 登录接口本身不需要令牌
            self._handle_login(self._read_json())
            return
        if not self._authed():
            self._json({"error": "unauthorized", "need_login": bool(self._password())}, 401)
            return
        self._api_post(route, self._read_json())

    # -- 口令鉴权 ----------------------------------------------------------
    def _password(self) -> str:
        return (self.cfg["web"].get("password") or "").strip()

    def _serve_login(self, error: str = "") -> None:
        """极简登录页（内联，不依赖静态资源）。"""
        msg = (
            f'<p class="err">{html.escape(error)}</p>' if error
            else '<p class="hint">请输入配置中设置的 Web 访问密码。</p>'
        )
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>grqc-daily 登录</title><style>
body{{margin:0;height:100vh;display:flex;align-items:center;justify-content:center;
background:#f4f6f9;font-family:"Source Han Sans SC","Noto Sans CJK SC","PingFang SC",
"Microsoft YaHei",sans-serif;color:#1c2431}}
.box{{background:#fff;border:1px solid #e2e8f0;border-radius:10px;padding:28px 30px;
width:340px;box-shadow:0 6px 24px rgba(20,30,50,.08)}}
h1{{font-size:17px;margin:0 0 4px}}p{{margin:0 0 16px;font-size:12.5px;color:#66748a}}
input{{width:100%;box-sizing:border-box;padding:9px 11px;border:1px solid #cfd8e3;
border-radius:6px;font-size:13px;font-family:inherit}}
button{{width:100%;margin-top:12px;padding:9px;border:0;border-radius:6px;background:#4a9eff;
color:#fff;font-size:13px;cursor:pointer;font-family:inherit}}
.err{{color:#c0392b}}.hint{{color:#66748a}}</style></head><body>
<form class="box" method="post" action="/login" id="f">
<h1>grqc-daily</h1>{msg}
<input type="password" name="password" id="p" placeholder="Web 访问密码" autofocus autocomplete="current-password">
<button type="submit">进入配置界面</button></form>
<script>
document.getElementById('f').addEventListener('submit',function(e){{
  e.preventDefault();
  fetch('/api/login',{{method:'POST',headers:{{'Content-Type':'application/json'}},
    body:JSON.stringify({{password:document.getElementById('p').value}})}})
    .then(function(r){{return r.json().then(function(d){{return {{ok:r.ok,d:d}};}});}})
    .then(function(res){{
      if(res.ok){{location.href='/index.html';}}
      else{{document.querySelector('.err,.hint').outerHTML='<p class="err">密码错误</p>';}}
    }})
    .catch(function(){{document.querySelector('.err,.hint').outerHTML='<p class="err">无法连接服务</p>';}});
}});
</script></body></html>"""
        data = page.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _handle_login(self, body: dict) -> None:
        expect = self._password()
        given = str(body.get("password") or "")
        if not expect:
            self._json({"ok": True, "note": "未设置密码"}, 200)
            return
        if given and hmac.compare_digest(given, expect):
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"grqc_token={self.token}; Path=/; SameSite=Strict; HttpOnly")
            payload = b'{"ok":true}'
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        time.sleep(0.4)          # 轻微延时，降低暴力破解速度
        self._json({"ok": False, "error": "密码错误"}, 401)

    # -- API -------------------------------------------------------------
    def _api_get(self, route: str) -> None:
        cfg, store = self.cfg, self.store
        if route == "config":
            self._json(self._config_payload())
            return
        if route == "categories":
            cats = load_categories(cfg)
            self._json({"groups": category_groups(cats), "total": len(cats)})
            return
        if route == "themes":
            self._json({"themes": load_themes(cfg)})
            return
        if route == "providers":
            self._json({"providers": describe_all()})
            return
        if route == "status":
            from .status import status_dict

            self._json(status_dict(cfg, store))
            return
        if route == "job":
            self._json(JOBS.snapshot())
            return
        if route == "reports":
            self._json({"reports": store.list_reports("daily", limit=200),
                        "monthly": store.list_reports("monthly", limit=36)})
            return
        self._json({"error": "unknown route"}, 404)

    def _api_post(self, route: str, body: dict) -> None:
        cfg, store = self.cfg, self.store
        if route == "config":
            patch = body.get("config") or {}
            cfg.update(patch)
            keys = body.get("api_keys") or {}
            for provider, key in keys.items():
                if key and not key.startswith("••"):
                    cfg.set_api_key(provider, key)
            cfg.save()
            self._json({"ok": True, "config": self._config_payload()})
            return
        if route == "test-connection":
            provider = (body.get("provider") or cfg["llm"]["provider"]).strip()
            tmp = cfg.as_dict()
            tmp["llm"]["provider"] = provider
            if body.get("base_url"):
                tmp["llm"]["base_url"] = body["base_url"]
            if body.get("model"):
                tmp["llm"]["model"] = body["model"]
            probe = Config(path=cfg.path, overrides={"llm": tmp["llm"]})
            probe._secrets = cfg._secrets
            client = LLMClient(probe, store)
            if body.get("api_key") and not str(body["api_key"]).startswith("••"):
                client.api_key = body["api_key"]
            self._json(client.test_connection())
            return
        if route == "run":
            kind = body.get("kind") or "daily"
            day = body.get("day") or None
            month = body.get("month") or None
            use_llm = bool(body.get("use_llm", True))

            def task(progress):
                if kind == "monthly":
                    from .report_monthly import run_monthly

                    paths = run_monthly(cfg, store, month=month, progress=progress)
                else:
                    from .report_daily import run_daily

                    paths = run_daily(cfg, store, day=day, use_llm=use_llm, progress=progress)
                progress("完成，共生成 %d 份报告" % len(paths))
                for p in paths:
                    progress("  " + str(p))

            if not JOBS.start(kind, task):
                self._json({"ok": False, "error": "已有任务在运行"}, 409)
                return
            self._json({"ok": True})
            return
        self._json({"error": "unknown route"}, 404)

    def _config_payload(self) -> dict:
        cfg = self.cfg
        data = cfg.as_dict()
        # 密钥只回显掩码
        data["llm"]["api_key_masked"] = {
            p: cfg.mask_key(p) for p in ("deepseek", "moonshot", "openai", "anthropic", "custom")
        }
        data["llm"]["api_key_source"] = {
            p: cfg.key_source(p) for p in ("deepseek", "moonshot", "openai", "anthropic", "custom")
        }
        data["_meta"] = {
            "home": str(cfg.home),
            "project_root": str(PROJECT_ROOT),
            "reports_dir": str(cfg.reports_dir),
            "data_dir": str(cfg.data_dir),
            "pdf_backends": _backends(),
        }
        return data


def _backends() -> list[str]:
    try:
        from .pdf import available_backends

        return available_backends()
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
def serve(cfg: Config, store: Store, *, host: str | None = None, port: int | None = None,
          open_browser: bool = True) -> None:
    web = cfg["web"]
    host = host or web.get("host", "127.0.0.1")
    port = int(port or web.get("port", 8765))
    if host not in ("127.0.0.1", "localhost", "::1") and not web.get("password"):
        raise SystemExit(
            "拒绝启动：绑定非回环地址时必须先在配置里设置 web.password，"
            "否则同网段任何人都能改动你的模型密钥。"
        )
    token = cfg.session_token()

    Handler.cfg = cfg
    Handler.store = store
    Handler.token = token

    httpd = ThreadingHTTPServer((host, port), Handler)
    url = f"http://{host}:{port}/?token={token}"
    print("=" * 68)
    print("  grqc-daily 配置界面已启动")
    print(f"  地址：{url}")
    print("  （令牌已写入 URL，仅本进程有效；请勿分享该链接）")
    print("=" * 68)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
