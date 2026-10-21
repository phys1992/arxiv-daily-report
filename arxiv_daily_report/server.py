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
    server_version = "arxiv-daily-report"
    cfg: Config
    store: Store
    token: str
    require_token: bool = False       # 由 serve/daemon 按配置设置

    # -- 工具 ------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 静默默认访问日志
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _token_ok(self) -> bool:
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        if params.get("token", [""])[0] == self.token:
            return True
        cookie = self.headers.get("Cookie", "")
        m = re.search(r"adr_token=([^;]+)", cookie)
        if m and m.group(1) == self.token:
            return True
        header = self.headers.get("X-Auth-Token", "")
        return bool(header) and header == self.token

    def _authed(self) -> bool:
        """鉴权规则（默认与 v2rayA 一类本地工具一致：回环地址即信任）。

          1. 带了正确令牌 -> 放行；
          2. 设了密码     -> 必须登录；
          3. 其余情况     -> 放行。
        第 3 条之所以安全：对外监听时启动阶段就强制要求密码，
        所以"没密码还能访问"只可能发生在只监听 127.0.0.1 的时候。
        想恢复"即使是本机也必须带令牌"，把 web.require_token 设为 true。
        """
        if self._token_ok():
            return True
        if self._password():
            return False
        return not Handler.require_token

    def _reject_host(self) -> None:
        """Host 不合法时的 403。

        注意：HTTP 状态行的原因短语历史上只能是 latin-1，塞中文会直接抛
        UnicodeEncodeError（连接被掐断，浏览器看到"空回复"）。所以原因短语
        一律用英文，中文说明放进 UTF-8 的响应体。
        """
        body = ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<title>403</title></head><body style=\"font-family:system-ui,sans-serif;"
                "max-width:38rem;margin:12vh auto;line-height:1.75\">"
                "<h1>403 拒绝访问</h1>"
                "<p>请求的 <code>Host</code> 头不是本机地址，已拒绝。</p>"
                "<p>这是为了防止 DNS rebinding：恶意网页可以把域名解析到 127.0.0.1，"
                "再借浏览器访问本机的配置界面。请用 "
                "<code>arxiv-daily-report status</code> 打印出的地址访问。</p>"
                "</body></html>").encode("utf-8")
        self.send_response(403, "Forbidden")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _host_ok(self) -> bool:
        """校验 Host 头，挡掉 DNS rebinding。

        免令牌之后这一步是必需的：恶意网页可以把自己的域名解析到 127.0.0.1，
        再用浏览器去打本机服务；此时 Host 是攻击者的域名，而正常访问只会是
        127.0.0.1 / localhost / 配置里写的主机名。
        """
        raw = (self.headers.get("Host") or "").strip()
        if not raw:
            return True                     # HTTP/1.0 客户端可能不带 Host
        if raw.startswith("["):             # IPv6 字面量 [::1]:8765
            name = raw[1:raw.find("]")] if "]" in raw else raw.strip("[]")
        elif raw.count(":") == 1:
            name = raw.rsplit(":", 1)[0]
        else:
            name = raw
        name = name.strip().lower()
        if name in ("127.0.0.1", "localhost", "::1", "0.0.0.0", "::"):
            return True
        configured = str(self.cfg["web"].get("host", "127.0.0.1")).strip().lower()
        if configured in ("0.0.0.0", "::"):
            return True                     # 对外监听：地址形态本来就多样，由密码把关
        return name == configured

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
        base = self.cfg.web_dir
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
        if not self._host_ok():
            self._reject_host()
            return
        u = urllib.parse.urlparse(self.path)
        path = u.path
        if path in ("/", "/index.html"):
            params = urllib.parse.parse_qs(u.query)
            token = params.get("token", [""])[0]
            if token and token == self.token:
                self.send_response(302)
                self.send_header("Set-Cookie", f"adr_token={token}; Path=/; SameSite=Strict")
                self.send_header("Location", "/index.html")
                self.end_headers()
                return
            if not self._authed():
                if self._password():
                    self._serve_login()          # 设了密码 -> 登录页
                else:
                    self._serve_hint()           # 要求令牌但没带 -> 说明页
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
        if not self._host_ok():
            self._json({"error": "bad host", "detail": "Host 头不是本机地址"}, 403)
            return
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

    def _serve_hint(self) -> None:
        """没设密码又没带令牌时的提示页。

        常驻服务（systemd）在后台跑，用户看不到启动时打印的地址，
        所以这里必须说清楚怎么把那个地址找出来，而不是甩一个 403。
        """
        body = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>需要访问令牌</title><style>
body{font-family:system-ui,sans-serif;max-width:40rem;margin:12vh auto;padding:0 1.2rem;
     color:#1c2430;line-height:1.75}
h1{font-size:1.35rem}
code,pre{background:#f2f5f9;border-radius:6px;font-size:.92rem}
code{padding:.12rem .35rem}
pre{padding:.7rem .9rem;overflow-x:auto}
li{margin:.35rem 0}
</style></head><body>
<h1>需要访问令牌</h1>
<p>配置界面只接受带令牌的地址（防止同机其他程序改动你的模型密钥）。任选一种方式取得：</p>
<ol>
  <li>命令行直接打印当前地址：<pre>arxiv-daily-report status</pre></li>
  <li>看常驻服务的日志（systemd 用户服务）：<pre>journalctl --user -u arxiv-daily-report | grep 在浏览器里打开</pre></li>
  <li>临时起一个前台实例：<pre>arxiv-daily-report serve</pre></li>
</ol>
<p>也可以给 Web 界面设一个密码，之后直接打开
<code>http://127.0.0.1:%(port)s</code> 就能登录：</p>
<pre>arxiv-daily-report serve   # 在「界面设置」里设置 web.password</pre>
</body></html>""" % {"port": self.server.server_address[1]}
        raw = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def _serve_login(self, error: str = "") -> None:
        """极简登录页（内联，不依赖静态资源）。"""
        msg = (
            f'<p class="err">{html.escape(error)}</p>' if error
            else '<p class="hint">请输入配置中设置的 Web 访问密码。</p>'
        )
        page = f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>arxiv-daily-report 登录</title><style>
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
<h1>arxiv-daily-report</h1>{msg}
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
            self.send_header("Set-Cookie", f"adr_token={self.token}; Path=/; SameSite=Strict; HttpOnly")
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

            self._json(status_dict(cfg, store))     # 时间已在 status_dict 里本地化
            return
        if route == "job":
            self._json(JOBS.snapshot())
            return
        if route == "reports":
            from .status import fmt_local_time

            tz = cfg["schedule"].get("timezone", "Asia/Shanghai")
            def _local(rows):
                for row in rows:
                    if row.get("generated_at"):
                        row["generated_at"] = fmt_local_time(row["generated_at"], tz)
                return rows
            self._json({"reports": _local(store.list_reports("daily", limit=200)),
                        "monthly": _local(store.list_reports("monthly", limit=36))})
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
            vkey = body.get("verify_api_key")
            if vkey is not None and not str(vkey).startswith("••"):
                cfg.set_verify_api_key(str(vkey).strip())
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
        data["llm"]["verify"]["api_key_masked"] = cfg.mask_verify_key()
        data["llm"]["verify"]["api_key_source"] = cfg.verify_key_source()
        data["llm"]["api_key_source"] = {
            p: cfg.key_source(p) for p in ("deepseek", "moonshot", "openai", "anthropic", "custom")
        }
        data["_meta"] = {
            "config_dir": str(cfg.config_home),
            "data_dir": str(cfg.data_dir),
            "cache_dir": str(cfg.cache_dir),
            "reports_dir": str(cfg.reports_dir),
            "mode": "portable" if cfg.portable else "system",
            "project_root": str(PROJECT_ROOT),
            "version": __import__("arxiv_daily_report", fromlist=["__version__"]).__version__,
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
def token_required(cfg: Config) -> bool:
    """是否必须带令牌才能访问。

    默认不需要——只监听回环地址时就信任本机（与 v2rayA 等本地工具一致）。
    设了密码、或显式打开 web.require_token 时才需要。
    """
    if (cfg["web"].get("password") or "").strip():
        return True
    return bool(cfg["web"].get("require_token", False))


def web_url(cfg: Config, host: str | None = None, port: int | None = None) -> str:
    """配置界面地址。

    不需要令牌时就是干净的 http://127.0.0.1:8765/；
    需要时附上令牌（保存在 secrets.json，持久化，任何进程算出来都一样）。
    """
    web = cfg["web"]
    host = host or web.get("host", "127.0.0.1")
    port = int(port or web.get("port", 8765))
    shown = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    base = f"http://{shown}:{port}/"
    if not token_required(cfg):
        return base
    return f"{base}?token={cfg.session_token()}"


def make_server(cfg: Config, store: Store, host: str | None = None,
                port: int | None = None) -> tuple[ThreadingHTTPServer, str]:
    """创建（但不启动）HTTP 服务。返回 (httpd, url)。"""
    web = cfg["web"]
    host = host or web.get("host", "127.0.0.1")
    port = int(port or web.get("port", 8765))
    if host not in ("127.0.0.1", "localhost", "::1") and not web.get("password"):
        raise SystemExit(
            "拒绝启动：绑定非回环地址时必须先在配置里设置 web.password，"
            "否则同网段任何人都能改动你的模型密钥。"
        )
    Handler.cfg = cfg
    Handler.store = store
    Handler.token = cfg.session_token()
    Handler.require_token = token_required(cfg)
    httpd = ThreadingHTTPServer((host, port), Handler)
    return httpd, web_url(cfg, host, port)


def announce(url: str, *, need_token: bool = False, stream=print) -> None:
    """把访问方式清楚地印出来（前台命令与 systemd 日志共用）。"""
    stream("=" * 68)
    stream("  arxiv-daily-report 配置界面")
    stream(f"  在浏览器里打开：{url}")
    if need_token:
        stream("  该地址里的令牌是访问凭据，请勿分享。")
    else:
        stream("  无需令牌，直接打开即可（仅监听本机回环地址）。")
    stream("  随时可用 `arxiv-daily-report status` 重新查看这个地址。")
    stream("=" * 68)


def start_web_thread(cfg: Config, store: Store, host: str | None = None,
                     port: int | None = None):
    """在后台线程里提供配置界面，供 daemon 使用。返回 (httpd 或 None)。"""
    import threading

    try:
        httpd, url = make_server(cfg, store, host, port)
    except SystemExit:
        raise
    except OSError as exc:
        log.warning("配置界面未启动（%s）。想改配置就直接编辑 %s，"
                    "或先停掉占用 %s:%s 的进程。",
                    exc, cfg.path, cfg["web"].get("host"), cfg["web"].get("port"))
        return None
    t = threading.Thread(target=httpd.serve_forever, name="adr-web", daemon=True)
    t.start()
    announce(url, need_token=token_required(cfg), stream=lambda m: log.info("%s", m))
    return httpd


def serve(cfg: Config, store: Store, *, host: str | None = None, port: int | None = None,
          open_browser: bool = True) -> None:
    try:
        httpd, url = make_server(cfg, store, host, port)
    except OSError as exc:
        raise SystemExit(
            f"端口被占用，无法启动配置界面：{exc}\n"
            f"  可能已经有一个实例在运行；先执行 `arxiv-daily-report status` 查看地址，\n"
            f"  或用 `--port 8766` 换一个端口。"
        ) from exc
    announce(url, need_token=token_required(cfg))
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
