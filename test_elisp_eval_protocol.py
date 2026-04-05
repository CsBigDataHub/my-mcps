#!/usr/bin/env python3
"""Protocol-level tests for elisp-eval-server.py _handle().

Run from repo root:
    python3 elisp-mcp/test_elisp_eval_protocol.py
    python3 -m unittest discover -s elisp-mcp -p 'test_*.py' -v
"""

import importlib.util
import json
import os
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "elisp-eval-server.py")

spec = importlib.util.spec_from_file_location("elisp_eval_server", _MOD_PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

_handle = mod._handle


class _Base(unittest.TestCase):
    """Helpers shared by all test cases."""

    def assert_json_serializable(self, resp):
        if resp is not None:
            try:
                json.dumps(resp)
            except (TypeError, ValueError) as exc:
                self.fail(f"Response is not JSON-serializable: {exc}\n{resp!r}")

    def assert_error_code(self, resp, code):
        self.assertIsNotNone(resp)
        self.assertIn("error", resp)
        self.assertEqual(resp["error"]["code"], code)
        self.assert_json_serializable(resp)

    def assert_success(self, resp):
        self.assertIsNotNone(resp)
        self.assertIn("result", resp)
        self.assertNotIn("error", resp)
        self.assert_json_serializable(resp)

    def assert_dropped(self, resp):
        self.assertIsNone(resp)


class TestJsonRpcValidation(_Base):
    def test_wrong_jsonrpc_version(self):
        resp = _handle({"jsonrpc": "1.0", "id": 1, "method": "ping"})
        self.assert_error_code(resp, -32600)
        self.assertEqual(resp["id"], 1)

    def test_missing_jsonrpc_field(self):
        resp = _handle({"id": 1, "method": "ping"})
        self.assert_error_code(resp, -32600)

    def test_non_string_method(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": 5})
        self.assert_error_code(resp, -32600)

    def test_missing_method(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1})
        self.assert_error_code(resp, -32600)

    def test_bad_jsonrpc_no_id_dropped(self):
        resp = _handle({"jsonrpc": "1.0", "method": "ping"})
        self.assert_dropped(resp)

    def test_bad_jsonrpc_with_bad_id_uses_null(self):
        resp = _handle({"jsonrpc": "1.0", "id": True, "method": "ping"})
        self.assert_error_code(resp, -32600)
        self.assertIsNone(resp["id"])


class TestIdValidation(_Base):
    def test_null_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": None, "method": "initialize", "params": {}}
        )
        self.assert_error_code(resp, -32600)

    def test_bool_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": True, "method": "initialize", "params": {}}
        )
        self.assert_error_code(resp, -32600)

    def test_float_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1.5, "method": "initialize", "params": {}}
        )
        self.assert_error_code(resp, -32600)

    def test_int_id_accepted(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assert_success(resp)
        self.assertEqual(resp["id"], 1)

    def test_string_id_accepted(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": "abc", "method": "initialize", "params": {}}
        )
        self.assert_success(resp)
        self.assertEqual(resp["id"], "abc")


class TestNotificationMismatch(_Base):
    def test_notification_with_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}
        )
        self.assert_error_code(resp, -32600)

    def test_request_method_without_id_dropped(self):
        resp = _handle({"jsonrpc": "2.0", "method": "initialize", "params": {}})
        self.assert_dropped(resp)

    def test_tools_list_without_id_dropped(self):
        resp = _handle({"jsonrpc": "2.0", "method": "tools/list"})
        self.assert_dropped(resp)

    def test_tools_call_without_id_dropped(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {
                    "name": "emacs-elisp-eval",
                    "arguments": {"code": "(+ 1 1)"},
                },
            }
        )
        self.assert_dropped(resp)

    def test_cancelled_notification_without_id_dropped(self):
        resp = _handle({"jsonrpc": "2.0", "method": "notifications/cancelled"})
        self.assert_dropped(resp)

    def test_cancelled_notification_with_id_is_unknown_method(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "notifications/cancelled"})
        self.assert_error_code(resp, -32601)


class TestParamsValidation(_Base):
    def test_params_array_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        )
        self.assert_error_code(resp, -32600)

    def test_params_omitted_ok(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assert_success(resp)

    def test_arguments_array_rejected(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "emacs-elisp-eval", "arguments": []},
            }
        )
        self.assert_error_code(resp, -32602)

    def test_code_must_be_string(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "emacs-elisp-eval", "arguments": {"code": 123}},
            }
        )
        self.assert_error_code(resp, -32602)


class TestMethodDispatch(_Base):
    def test_initialize(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assert_success(resp)
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], "emacs-elisp")

    def test_notifications_initialized(self):
        resp = _handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assert_dropped(resp)

    def test_tools_list(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assert_success(resp)
        tools = resp["result"]["tools"]
        self.assertIsInstance(tools, list)
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "emacs-elisp-eval")

    def test_tools_call_accepts_legacy_alias(self):
        original = mod._eval_elisp
        mod._eval_elisp = lambda code: {"content": [{"type": "text", "text": code}]}
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "elisp-eval", "arguments": {"code": "(+ 1 1)"}},
                }
            )
        finally:
            mod._eval_elisp = original
        self.assert_success(resp)

    def test_ping(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "ping"})
        self.assert_success(resp)

    def test_unknown_tool_returns_error(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "nope"},
            }
        )
        self.assert_error_code(resp, -32602)

    def test_unknown_method_with_id(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "bogus/thing"})
        self.assert_error_code(resp, -32601)


class TestEvalImplementation(_Base):
    def test_inline_wrapper_uses_base64_payload(self):
        wrapper = mod._build_eval_wrapper('(message "hi")', use_temp_file=False)
        self.assertIn("base64-decode-string", wrapper)
        self.assertNotIn("insert-file-contents", wrapper)
        self.assertNotIn('(message "hi")', wrapper)

    def test_tempfile_wrapper_uses_insert_file_contents(self):
        wrapper = mod._build_eval_wrapper("/tmp/example.el", use_temp_file=True)
        self.assertIn("insert-file-contents", wrapper)
        self.assertNotIn("base64-decode-string", wrapper)
        self.assertIn("/tmp/example.el", wrapper)

    def test_parse_emacs_json_string_payload(self):
        raw = json.dumps(json.dumps({"result": "42", "messages": "hello"}))
        payload = mod._parse_emacs_eval_output(raw)
        self.assertEqual(payload["result"], "42")
        self.assertEqual(payload["messages"], "hello")

    def test_parse_emacs_json_object_payload(self):
        payload = mod._parse_emacs_eval_output('{"result":"nil","messages":null}')
        self.assertEqual(payload["result"], "nil")
        self.assertIsNone(payload["messages"])

    def test_eval_elisp_inline_returns_existing_content_shape(self):
        with mock.patch.object(
            mod,
            "_run_emacsclient",
            return_value={
                "returncode": 0,
                "stdout": json.dumps(
                    json.dumps({"result": "42", "messages": "from messages"})
                ),
                "stderr": "",
            },
        ) as run_emacsclient:
            result = mod._eval_elisp("(+ 40 2)")

        self.assertEqual(result["content"][0]["text"], "42")
        self.assertEqual(
            result["content"][1]["text"], "--- *Messages* ---\nfrom messages"
        )
        wrapper = run_emacsclient.call_args.args[0]
        self.assertIn("base64-decode-string", wrapper)
        self.assertNotIn("insert-file-contents", wrapper)

    def test_eval_elisp_falls_back_to_tempfile_for_large_payloads(self):
        code = "x" * (mod.INLINE_CODE_MAX_BYTES + 1)
        file_handle = mock.mock_open()
        with mock.patch.object(
            mod.tempfile, "mkstemp", return_value=(123, "/tmp/fallback.el")
        ), mock.patch.object(mod.os, "fdopen", file_handle), mock.patch.object(
            mod.os, "unlink"
        ), mock.patch.object(
            mod,
            "_run_emacsclient",
            return_value={
                "returncode": 0,
                "stdout": json.dumps(json.dumps({"result": "ok", "messages": None})),
                "stderr": "",
            },
        ) as run_emacsclient:
            result = mod._eval_elisp(code)

        self.assertEqual(result["content"][0]["text"], "ok")
        wrapper = run_emacsclient.call_args.args[0]
        self.assertIn("insert-file-contents", wrapper)
        self.assertNotIn("base64-decode-string", wrapper)


class TestMainLoop(_Base):
    def _simulate_line(self, line: str) -> str | None:
        line = line.strip()
        if not line:
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32700, "message": "Parse error"},
                }
            )
        if not isinstance(msg, dict):
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Invalid request: expected JSON object",
                    },
                }
            )
        resp = _handle(msg)
        if resp is None:
            return None
        return json.dumps(resp)

    def test_malformed_json(self):
        out = self._simulate_line("{bad json")
        self.assertIsNotNone(out)
        resp = json.loads(out)
        self.assert_error_code(resp, -32700)

    def test_json_array(self):
        out = self._simulate_line("[1, 2, 3]")
        self.assertIsNotNone(out)
        resp = json.loads(out)
        self.assert_error_code(resp, -32600)

    def test_empty_line_ignored(self):
        out = self._simulate_line("")
        self.assertIsNone(out)

    def test_valid_request_through_main(self):
        out = self._simulate_line('{"jsonrpc":"2.0","id":1,"method":"ping"}')
        self.assertIsNotNone(out)
        resp = json.loads(out)
        self.assert_success(resp)


if __name__ == "__main__":
    unittest.main()
