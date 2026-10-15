"""多厂商模型适配器测试。

用一个本地 mock HTTP 服务器同时模拟两种线上协议：
  * OpenAI Chat Completions（DeepSeek / Kimi / OpenAI / 本地服务共用）
  * Anthropic Messages（Claude 专用，请求与响应结构都不同）

这样无需任何真实密钥就能验证请求构造、响应解析与用量记账是否正确，
同时证明「自定义（OpenAI 兼容）」可以指向本地服务。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from arxiv_daily_report.config import Config  # noqa: E402
from arxiv_daily_report.llm.client import LLMClient  # noqa: E402
from arxiv_daily_report.store import Store  # noqa: E402

RECORDED: list[dict] = []


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静默
        pass

    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        RECORDED.append({"method": "GET", "path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()}})
        if self.path.endswith("/models"):
            self._send({"data": [{"id": "mock-small"}, {"id": "mock-large"}]})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        RECORDED.append({"method": "POST", "path": self.path,
                         "headers": {k.lower(): v for k, v in self.headers.items()},
                         "body": payload})
        if self.path.endswith("/chat/completions"):
            self._send({
                "choices": [{"finish_reason": "stop",
                             "message": {"role": "assistant",
                                         "content": json.dumps({"ok": True, "wire": "openai"})}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            })
        elif self.path.endswith("/messages"):
            self._send({
                "content": [{"type": "text", "text": json.dumps({"ok": True, "wire": "anthropic"})}],
                "usage": {"input_tokens": 13, "output_tokens": 5},
            })
        else:
            self._send({"error": "not found"}, 404)


def setUpModule() -> None:
    global SERVER, THREAD, BASE
    SERVER = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    BASE = f"http://127.0.0.1:{SERVER.server_address[1]}/v1"
    THREAD = threading.Thread(target=SERVER.serve_forever, daemon=True)
    THREAD.start()


def tearDownModule() -> None:
    SERVER.shutdown()
    SERVER.server_close()


class LLMAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        RECORDED.clear()
        self.cfg = Config(path=Path("/tmp/adr-test-config.json"),
                          overrides={"llm": {"base_url": BASE, "model": "mock-small",
                                             "max_tokens": 256, "disable_thinking": True}})

    def _client(self, provider: str) -> LLMClient:
        c = LLMClient(self.cfg, None, provider_key=provider)
        c.base_url = BASE
        c.api_key = "test-key"
        return c

    # -- OpenAI 线 --------------------------------------------------------
    def test_openai_wire_request_and_response(self):
        client = self._client("custom")
        out = client.chat_json("系统提示", "用户内容", schema_hint='{"ok":true}')
        self.assertEqual(out, {"ok": True, "wire": "openai"})
        req = [r for r in RECORDED if r["method"] == "POST"][-1]
        self.assertTrue(req["path"].endswith("/chat/completions"))
        self.assertEqual(req["headers"].get("authorization"), "Bearer test-key")
        self.assertEqual(req["body"]["messages"][0]["role"], "system")
        self.assertEqual(req["body"]["messages"][1]["content"], "用户内容")
        self.assertEqual(req["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(client.last_usage.prompt_tokens, 11)
        self.assertEqual(client.last_usage.completion_tokens, 7)

    def test_openai_wire_lists_models(self):
        models = self._client("custom").list_models()
        self.assertEqual(models, ["mock-large", "mock-small"])

    # -- Anthropic 线 -----------------------------------------------------
    def test_anthropic_wire_uses_system_field_and_x_api_key(self):
        client = self._client("anthropic")
        out = client.chat_json("系统提示", "用户内容", schema_hint='{"ok":true}')
        self.assertEqual(out, {"ok": True, "wire": "anthropic"})
        req = [r for r in RECORDED if r["method"] == "POST"][-1]
        self.assertTrue(req["path"].endswith("/messages"))
        # 鉴权头与版本头
        self.assertEqual(req["headers"].get("x-api-key"), "test-key")
        self.assertEqual(req["headers"].get("anthropic-version"), "2023-06-01")
        self.assertNotIn("authorization", req["headers"])
        # system 是独立顶层字段，不在 messages 里
        self.assertIn("系统提示", req["body"]["system"])
        self.assertNotIn("system", [m.get("role") for m in req["body"]["messages"]])
        self.assertIn("max_tokens", req["body"])
        # Anthropic 不支持 response_format，不应下发
        self.assertNotIn("response_format", req["body"])
        # 响应走 content[0].text，用量字段名不同
        self.assertEqual(client.last_usage.prompt_tokens, 13)
        self.assertEqual(client.last_usage.completion_tokens, 5)

    def test_thinking_disabled_only_for_deepseek(self):
        custom = self._client("custom")
        custom.chat_json("s", "u")
        self.assertNotIn("thinking", [r for r in RECORDED if r["method"] == "POST"][-1]["body"])

        cfg = Config(path=Path("/tmp/adr-test-config.json"),
                     overrides={"llm": {"base_url": BASE, "model": "mock",
                                        "disable_thinking": True}})
        deep = LLMClient(cfg, None, provider_key="deepseek")
        deep.base_url = BASE
        deep.api_key = "k"
        deep.chat_json("s", "u")
        body = [r for r in RECORDED if r["method"] == "POST"][-1]["body"]
        self.assertEqual(body.get("thinking"), {"type": "disabled"})

    def test_usage_recorded_in_store(self):
        store = Store(Path("/tmp/adr-test-state.sqlite3"))
        try:
            store.conn.execute("DELETE FROM token_ledger")
            store.conn.commit()
            client = LLMClient(self.cfg, store, provider_key="custom")
            client.base_url = BASE
            client.api_key = "k"
            client.chat_json("s", "u")
            usage = store.usage_since("2000-01-01T00:00:00Z")
            self.assertEqual(usage["calls"], 1)
            self.assertEqual(usage["prompt_tokens"], 11)
        finally:
            store.close()

    # -- 容错解析 ---------------------------------------------------------
    def test_json_parsing_tolerates_fences_and_prose(self):
        from arxiv_daily_report.llm.client import _parse_json

        self.assertEqual(_parse_json('{"a":1}'), {"a": 1})
        self.assertEqual(_parse_json('```json\n{"a":2}\n```'), {"a": 2})
        self.assertEqual(_parse_json('好的：{"a":3} 以上'), {"a": 3})
        self.assertIsNone(_parse_json("完全不是 JSON"))
        self.assertIsNone(_parse_json(""))

    def test_missing_key_reported(self):
        cfg = Config(path=Path("/tmp/adr-test-config.json"),
                     overrides={"llm": {"provider": "openai", "base_url": BASE}})
        client = LLMClient(cfg, None)
        client.api_key = ""
        self.assertFalse(client.ready)
        from arxiv_daily_report.llm.client import LLMError

        with self.assertRaises(LLMError):
            client.chat_json("s", "u")


if __name__ == "__main__":
    unittest.main(verbosity=2)
