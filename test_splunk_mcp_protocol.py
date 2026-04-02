#!/usr/bin/env python3
"""Protocol-level tests for splunk-mcp.py _handle().

Run:  SPLUNK_HOST=example.com python3 test_splunk_mcp_protocol.py
      SPLUNK_HOST=example.com python3 -m unittest test_splunk_mcp_protocol -v
"""

import importlib.util
import json
import os
import threading
import unittest
from collections.abc import Callable
from types import ModuleType
from typing import Any, Protocol, cast
from unittest import mock

# Ensure SPLUNK_HOST is set so the module can be imported
os.environ.setdefault("SPLUNK_HOST", "example.com")

_HERE = os.path.dirname(os.path.abspath(__file__))
_MOD_PATH = os.path.join(_HERE, "splunk-mcp.py")


class _SplunkModule(Protocol):
    SPLUNK_HOST: str
    SERVER_NAME: str
    CANCELLED_ERROR_CODE: int
    sys: ModuleType
    _pending_lock: Any
    _pending_requests: dict[Any, threading.Event]
    _handle: Callable[[dict[str, Any]], Any]
    _search_async: Callable[..., dict[str, Any]]
    _do_search: Callable[..., dict[str, Any]]
    _do_indexes: Callable[..., dict[str, Any]]
    _do_metadata: Callable[..., dict[str, Any]]
    _splunk_get: Callable[..., tuple[int, dict[str, Any]]]


def _load_module(argv=None, env=None) -> _SplunkModule:
    argv = argv or ["splunk-mcp.py"]
    env = env or {"SPLUNK_HOST": "example.com"}
    with mock.patch.dict(os.environ, env, clear=False), mock.patch("sys.argv", argv):
        spec = importlib.util.spec_from_file_location("splunk_mcp", _MOD_PATH)
        if spec is None:
            raise AssertionError(f"Failed to create import spec for {_MOD_PATH}")
        module = importlib.util.module_from_spec(spec)
        exec_module = getattr(spec.loader, "exec_module", None)
        if exec_module is None:
            raise AssertionError("Import loader does not support exec_module")
        exec_module(module)
        return cast(_SplunkModule, module)


mod = _load_module()
_handle = mod._handle


class _Base(unittest.TestCase):
    """Helpers shared by all test cases."""

    def assert_json_serializable(self, resp):
        """Verify no sentinel or non-serializable object leaks into the response."""
        if resp is not None:
            try:
                json.dumps(resp)
            except (TypeError, ValueError) as e:
                self.fail(f"Response is not JSON-serializable: {e}\n{resp!r}")

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

    def require_text(self, value: str | None) -> str:
        if value is None:
            raise AssertionError("Expected a text value")
        return value


# ── Runtime configuration ─────────────────────────────────────────────────


class TestRuntimeConfig(_Base):
    """Import-time runtime config selection."""

    def test_defaults_server_name_to_splunk(self):
        fresh_mod = _load_module(env={"SPLUNK_HOST": "example.com"})
        self.assertEqual(fresh_mod.SPLUNK_HOST, "example.com")
        self.assertEqual(fresh_mod.SERVER_NAME, "splunk")

    def test_server_name_can_come_from_cli_args(self):
        fresh_mod = _load_module(
            argv=[
                "splunk-mcp.py",
                "--host",
                "stage.example.com",
                "--server-name",
                "splunk-nonprod",
            ],
            env={"SPLUNK_HOST": "prod.example.com", "MCP_SERVER_NAME": "ignored"},
        )
        self.assertEqual(fresh_mod.SPLUNK_HOST, "stage.example.com")
        self.assertEqual(fresh_mod.SERVER_NAME, "splunk-nonprod")

    def test_server_name_can_come_from_env_when_arg_missing(self):
        fresh_mod = _load_module(
            argv=["splunk-mcp.py", "--host", "envtest.example.com"],
            env={
                "SPLUNK_HOST": "prod.example.com",
                "MCP_SERVER_NAME": "splunk-from-env",
            },
        )
        self.assertEqual(fresh_mod.SPLUNK_HOST, "envtest.example.com")
        self.assertEqual(fresh_mod.SERVER_NAME, "splunk-from-env")

    def test_import_requires_host_from_arg_or_env(self):
        with self.assertRaises(SystemExit) as exc:
            _load_module(argv=["splunk-mcp.py"], env={"SPLUNK_HOST": ""})
        self.assertIn("SPLUNK_HOST env var required", str(exc.exception))


# ── Base JSON-RPC validation ──────────────────────────────────────────────


class TestJsonRpcValidation(_Base):
    """jsonrpc must be '2.0' and method must be a string."""

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

    def test_null_method(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": None})
        self.assert_error_code(resp, -32600)

    def test_missing_method(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1})
        self.assert_error_code(resp, -32600)

    def test_bad_jsonrpc_no_id_dropped(self):
        """Bad jsonrpc with no id: silently dropped (can't respond)."""
        resp = _handle({"jsonrpc": "1.0", "method": "ping"})
        self.assert_dropped(resp)

    def test_bad_jsonrpc_preserves_valid_id(self):
        """Error response uses the original id if it's a valid type."""
        resp = _handle({"jsonrpc": "1.0", "id": "abc", "method": "ping"})
        self.assert_error_code(resp, -32600)
        self.assertEqual(resp["id"], "abc")

    def test_bad_jsonrpc_with_bad_id_uses_null(self):
        """Error response uses null id if the original id is invalid."""
        resp = _handle({"jsonrpc": "1.0", "id": True, "method": "ping"})
        self.assert_error_code(resp, -32600)
        self.assertIsNone(resp["id"])


# ── ID validation ─────────────────────────────────────────────────────────


class TestIdValidation(_Base):
    """JSON-RPC 2.0: id must be string or integer when present."""

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

    def test_list_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": [1], "method": "initialize", "params": {}}
        )
        self.assert_error_code(resp, -32600)

    def test_dict_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": {"a": 1}, "method": "initialize", "params": {}}
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

    def test_zero_id_accepted(self):
        resp = _handle({"jsonrpc": "2.0", "id": 0, "method": "ping"})
        self.assert_success(resp)
        self.assertEqual(resp["id"], 0)

    def test_negative_id_accepted(self):
        resp = _handle({"jsonrpc": "2.0", "id": -1, "method": "ping"})
        self.assert_success(resp)
        self.assertEqual(resp["id"], -1)

    def test_empty_string_id_accepted(self):
        resp = _handle({"jsonrpc": "2.0", "id": "", "method": "ping"})
        self.assert_success(resp)
        self.assertEqual(resp["id"], "")


# ── Notification / request mismatch ───────────────────────────────────────


class TestNotificationMismatch(_Base):
    """Notification methods must not have id; request methods must have id."""

    def test_notification_with_id_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "notifications/initialized"}
        )
        self.assert_error_code(resp, -32600)

    def test_request_method_without_id_dropped(self):
        """Request method sent as notification — silently dropped (no id to respond with)."""
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
                "params": {"name": "splunk-search", "arguments": {"query": "x"}},
            }
        )
        self.assert_dropped(resp)

    def test_ping_without_id_dropped(self):
        resp = _handle({"jsonrpc": "2.0", "method": "ping"})
        self.assert_dropped(resp)

    def test_unknown_method_without_id_dropped(self):
        resp = _handle({"jsonrpc": "2.0", "method": "bogus/thing"})
        self.assert_dropped(resp)

    def test_notification_with_bad_params_dropped(self):
        """Notification with invalid params: silently dropped (no id to respond with)."""
        resp = _handle(
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": []}
        )
        self.assert_dropped(resp)

    def test_cancelled_notification_dropped(self):
        """notifications/cancelled without id: silently accepted."""
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": "123"},
            }
        )
        self.assert_dropped(resp)

    def test_cancelled_notification_with_id_rejected(self):
        """notifications/cancelled with id: protocol error (notifications must not have id)."""
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "notifications/cancelled",
                "params": {"requestId": "123"},
            }
        )
        self.assert_error_code(resp, -32600)

    def test_cancelled_notification_no_params_dropped(self):
        """notifications/cancelled without params: still silently accepted (per MCP spec)."""
        resp = _handle({"jsonrpc": "2.0", "method": "notifications/cancelled"})
        self.assert_dropped(resp)


# ── Params validation ─────────────────────────────────────────────────────


class TestParamsValidation(_Base):
    """params must be a dict (JSON object) when present."""

    def test_params_array_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []}
        )
        self.assert_error_code(resp, -32600)

    def test_params_string_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": "bad"}
        )
        self.assert_error_code(resp, -32600)

    def test_params_int_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": 42}
        )
        self.assert_error_code(resp, -32600)

    def test_params_null_rejected(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": None}
        )
        self.assert_error_code(resp, -32600)

    def test_params_omitted_ok(self):
        """Omitting params entirely defaults to {} and is valid."""
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assert_success(resp)

    def test_arguments_array_rejected(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "splunk-search", "arguments": []},
            }
        )
        self.assert_error_code(resp, -32602)


# ── Normal method dispatch ────────────────────────────────────────────────


class TestMethodDispatch(_Base):
    """Normal happy-path dispatch for all known methods."""

    def test_initialize(self):
        resp = _handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        self.assert_success(resp)
        result = resp["result"]
        self.assertEqual(result["protocolVersion"], "2024-11-05")
        self.assertEqual(result["serverInfo"]["name"], mod.SERVER_NAME)

    def test_notifications_initialized(self):
        resp = _handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.assert_dropped(resp)

    def test_tools_list(self):
        resp = _handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assert_success(resp)
        tools = resp["result"]["tools"]
        self.assertIsInstance(tools, list)
        names = {t["name"] for t in tools}
        self.assertIn("splunk-search", names)
        self.assertIn("splunk-indexes", names)
        self.assertIn("splunk-search-metadata", names)
        self.assertIn("splunk-server-info", names)
        by_name = {t["name"]: t for t in tools}
        self.assertEqual(
            by_name["splunk-search"]["inputSchema"]["properties"]["max_results"][
                "type"
            ],
            "integer",
        )
        self.assertEqual(
            by_name["splunk-indexes"]["inputSchema"]["properties"]["include_internal"][
                "type"
            ],
            "boolean",
        )
        self.assertEqual(by_name["splunk-server-info"]["inputSchema"]["properties"], {})

    def test_splunk_server_info_tool(self):
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "splunk-server-info", "arguments": {}},
            }
        )
        self.assert_success(resp)
        payload = json.loads(resp["result"]["content"][0]["text"])
        self.assertEqual(payload["server_name"], mod.SERVER_NAME)
        self.assertEqual(payload["splunk_host"], mod.SPLUNK_HOST)
        self.assertIsInstance(payload["pid"], int)
        self.assertEqual(payload["argv"], mod.sys.argv)

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


# ── Main loop integration (stdin/stdout) ──────────────────────────────────


class TestMainLoop(_Base):
    """Test the main loop's pre-_handle validation."""

    def _simulate_line(self, line: str) -> str | None:
        """Feed a raw line through the same logic as main(), return the response line or None."""
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
        resp = json.loads(self.require_text(out))
        self.assert_error_code(resp, -32700)

    def test_json_array(self):
        out = self._simulate_line("[1, 2, 3]")
        resp = json.loads(self.require_text(out))
        self.assert_error_code(resp, -32600)

    def test_json_string(self):
        out = self._simulate_line('"hello"')
        resp = json.loads(self.require_text(out))
        self.assert_error_code(resp, -32600)

    def test_json_number(self):
        out = self._simulate_line("42")
        resp = json.loads(self.require_text(out))
        self.assert_error_code(resp, -32600)

    def test_empty_line_ignored(self):
        out = self._simulate_line("")
        self.assertIsNone(out)

    def test_whitespace_line_ignored(self):
        out = self._simulate_line("   ")
        self.assertIsNone(out)

    def test_valid_request_through_main(self):
        out = self._simulate_line('{"jsonrpc":"2.0","id":1,"method":"ping"}')
        resp = json.loads(self.require_text(out))
        self.assert_success(resp)


# ── Cancellation support ─────────────────────────────────────────────────


class TestCancellationBehavior(_Base):
    """Test notification handling and direct cancellation-aware helpers."""

    def test_cancelled_notification_sets_matching_pending_event(self):
        event = threading.Event()
        with mod._pending_lock:
            mod._pending_requests[42] = event
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 42},
                }
            )
            self.assert_dropped(resp)
            self.assertTrue(event.is_set())
        finally:
            with mod._pending_lock:
                mod._pending_requests.pop(42, None)

    def test_cancelled_notification_unknown_request_id_dropped(self):
        """Unknown requestId is ignored without affecting other requests."""
        event = threading.Event()
        with mod._pending_lock:
            mod._pending_requests[42] = event
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 999},
                }
            )
            self.assert_dropped(resp)
            self.assertFalse(event.is_set())
        finally:
            with mod._pending_lock:
                mod._pending_requests.pop(42, None)

    def test_cancelled_notification_string_request_id_sets_matching_event(self):
        event = threading.Event()
        with mod._pending_lock:
            mod._pending_requests["abc-123"] = event
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": "abc-123"},
                }
            )
            self.assert_dropped(resp)
            self.assertTrue(event.is_set())
        finally:
            with mod._pending_lock:
                mod._pending_requests.pop("abc-123", None)

    def test_cancelled_notification_bool_request_id_ignored(self):
        event = threading.Event()
        with mod._pending_lock:
            mod._pending_requests[1] = event
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": True},
                }
            )
            self.assert_dropped(resp)
            self.assertFalse(event.is_set())
        finally:
            with mod._pending_lock:
                mod._pending_requests.pop(1, None)

    def test_cancelled_notification_missing_request_id_ignored(self):
        """notifications/cancelled with params but no requestId is harmless."""
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"reason": "user clicked stop"},
            }
        )
        self.assert_dropped(resp)

    def test_cancelled_notification_null_request_id_ignored(self):
        """notifications/cancelled with null requestId is harmless."""
        resp = _handle(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": None},
            }
        )
        self.assert_dropped(resp)

    def test_search_async_respects_pre_set_cancel_event(self):
        """_search_async returns cancelled immediately if event is already set."""
        event = threading.Event()
        event.set()
        result = mod._search_async("search index=main", cancel_event=event)
        self.assertIn("cancelled", result)
        self.assertTrue(result["cancelled"])

    def test_cancelled_search_produces_cancelled_dict(self):
        """_do_search returns cancelled dict when cancel event is pre-set."""
        event = threading.Event()
        event.set()
        result = mod._do_search({"query": "index=main"}, cancel_event=event)
        self.assertIn("cancelled", result)
        self.assertTrue(result["cancelled"])

    def test_handle_tools_call_returns_cancelled_error(self):
        """Full _handle() path: cancelled tools/call returns a JSON-RPC cancellation error."""
        # Monkey-patch _search_async to always return cancelled without network I/O.
        original = mod._search_async

        def always_cancelled(spl, params=None, max_results=100, cancel_event=None):
            return {"cancelled": True}

        mod._search_async = always_cancelled
        try:
            resp = _handle(
                {
                    "jsonrpc": "2.0",
                    "id": 999,
                    "method": "tools/call",
                    "params": {
                        "name": "splunk-search",
                        "arguments": {"query": "index=main"},
                    },
                }
            )
            self.assert_error_code(resp, mod.CANCELLED_ERROR_CODE)
            self.assertEqual(resp["id"], 999)
        finally:
            mod._search_async = original


class TestToolArgs(_Base):
    def test_do_search_rejects_negative_max_results(self):
        result = mod._do_search({"query": "index=main", "max_results": -1})
        self.assertTrue(result["isError"])
        self.assertIn("must be >= 1", result["content"][0]["text"])

    def test_do_search_rejects_empty_query(self):
        result = mod._do_search({"query": "   "})
        self.assertTrue(result["isError"])
        self.assertIn("non-empty string", result["content"][0]["text"])

    def test_do_indexes_accepts_boolean_include_internal(self):
        original = mod._splunk_get
        mod._splunk_get = lambda *args, **kwargs: (
            200,
            {
                "entry": [
                    {
                        "name": "_internal",
                        "content": {"totalEventCount": 1, "currentDBSizeMB": 2},
                    },
                    {
                        "name": "main",
                        "content": {"totalEventCount": 3, "currentDBSizeMB": 4},
                    },
                ]
            },
        )
        try:
            result = mod._do_indexes({"include_internal": True})
        finally:
            mod._splunk_get = original
        self.assertNotIn("isError", result)
        text = result["content"][0]["text"]
        self.assertIn("_internal", text)
        self.assertIn("main", text)

    def test_do_metadata_rejects_invalid_type(self):
        result = mod._do_metadata({"index": "main", "metadata_type": "users"})
        self.assertTrue(result["isError"])
        self.assertIn("metadata_type must be one of", result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
