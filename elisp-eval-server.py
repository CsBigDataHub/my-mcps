#!/usr/bin/env python3
"""MCP server for elisp evaluation via emacsclient.

Uses an inline base64 fast path for most requests and falls back to a temp file
for oversized payloads, avoiding shell-escaping issues in both modes.
"""

import base64
import json
import os
import subprocess
import sys
import tempfile

# ---------- Tool definition ----------

TOOL = {
    "name": "emacs-elisp-eval",
    "description": (
        "Evaluate Emacs Lisp code in the running Emacs server. Returns the result "
        "of evaluation along with any new *Messages* output produced during evaluation. "
        "State persists between calls. Only the return value of the last expression is "
        "captured. Write elisp naturally with no escaping needed. You can also read "
        "special buffers like *Messages*, *compilation*, etc. via "
        "(with-current-buffer BUF (buffer-string))."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Emacs Lisp code to evaluate.",
            },
        },
        "required": ["code"],
    },
}

# ---------- Elisp evaluation ----------

EMACSCLIENT = "/Applications/Emacs.app/Contents/MacOS/bin/emacsclient"
INLINE_CODE_MAX_BYTES = 256 * 1024


def _run_emacsclient(wrapper: str, timeout_s: float = 60) -> dict:
    """Run emacsclient and return a tool result payload."""
    try:
        proc = subprocess.run(
            [EMACSCLIENT, "--eval", wrapper],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {
            "content": [
                {
                    "type": "text",
                    "text": f"emacsclient timed out after {int(timeout_s)}s",
                }
            ],
            "isError": True,
        }
    except OSError as exc:
        return {
            "content": [{"type": "text", "text": str(exc)}],
            "isError": True,
        }
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def _elisp_string(value: str) -> str:
    """Return a JSON-escaped string literal compatible with Elisp string syntax."""
    return json.dumps(value)


def _build_eval_wrapper(payload: str, *, use_temp_file: bool) -> str:
    """Build Elisp wrapper for either inline base64 payloads or temp-file input."""
    if use_temp_file:
        source_expr = f"(insert-file-contents {_elisp_string(payload)})"
    else:
        source_expr = f"(insert (base64-decode-string {_elisp_string(payload)}))"

    return (
        "(progn\n"
        "  (require 'json)\n"
        '  (let* ((msgs-buf (get-buffer-create "*Messages*"))\n'
        "         (msgs-pos (with-current-buffer msgs-buf (point-max)))\n"
        "         (result (with-temp-buffer\n"
        f"                   {source_expr}\n"
        "                   (goto-char (point-min))\n"
        "                   (let (forms)\n"
        "                     (condition-case nil\n"
        "                         (while t (push (read (current-buffer)) forms))\n"
        "                       (end-of-file nil))\n"
        "                     (eval (cons 'progn (nreverse forms)) t))))\n"
        "         (new-msgs (with-current-buffer msgs-buf\n"
        "                     (let ((s (string-trim (buffer-substring-no-properties msgs-pos (point-max)))))\n"
        "                       (and (not (string-empty-p s)) s)))))\n"
        '    (json-encode (list (cons "result" (format "%S" result))\n'
        '                       (cons "messages" new-msgs)))))'
    )


def _parse_emacs_eval_output(stdout: str) -> dict:
    """Parse JSON emitted by the Emacs wrapper.

    emacsclient prints the return value of the evaluated expression. Since json-encode
    returns a string, emacsclient will print it as a quoted Lisp string. We need to
    parse it twice: once to get the string value, then again to get the JSON object.
    """
    stdout = stdout.strip()
    if not stdout:
        raise ValueError("Emacs response was empty")
    
    # First parse: get the string from the Lisp representation
    payload = json.loads(stdout)
    
    # If it's a string, parse it again to get the actual JSON object
    if isinstance(payload, str):
        payload = json.loads(payload)
    
    if not isinstance(payload, dict):
        raise ValueError("Emacs response was not a JSON object")
    return payload


def _format_eval_result(payload: dict) -> dict:
    """Translate wrapper payload into the existing MCP tool result shape."""
    result = payload.get("result")
    messages = payload.get("messages")
    if not isinstance(result, str):
        raise ValueError("Emacs response missing string result")

    content = [{"type": "text", "text": result}]
    if isinstance(messages, str) and messages:
        content.append({"type": "text", "text": f"--- *Messages* ---\n{messages}"})
    return {"content": content}


def _process_emacsclient_result(proc_result: dict) -> dict:
    """Convert a raw emacsclient result into an MCP tool result payload."""
    if proc_result.get("isError"):
        return proc_result

    proc_stdout = proc_result["stdout"]
    proc_stderr = proc_result["stderr"]
    proc_returncode = proc_result["returncode"]

    if proc_returncode == 0:
        try:
            return _format_eval_result(_parse_emacs_eval_output(proc_stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            combined = (proc_stdout + proc_stderr).strip()
            if combined:
                combined = f"{combined}\n\nFailed to parse structured Emacs response: {exc}"
            else:
                combined = f"Failed to parse structured Emacs response: {exc}"
            return {
                "content": [{"type": "text", "text": combined}],
                "isError": True,
            }

    combined = (proc_stdout + proc_stderr).strip()
    return {"content": [{"type": "text", "text": combined}], "isError": True}


def _eval_elisp(code: str) -> dict:
    """Evaluate Elisp via emacsclient, using inline payloads for the fast path."""
    code_bytes = code.encode("utf-8")
    if len(code_bytes) <= INLINE_CODE_MAX_BYTES:
        payload = base64.b64encode(code_bytes).decode("ascii")
        wrapper = _build_eval_wrapper(payload, use_temp_file=False)
        return _process_emacsclient_result(_run_emacsclient(wrapper, timeout_s=60))

    fd_code, path_code = tempfile.mkstemp(prefix="eca-elisp-", suffix=".el")
    try:
        with os.fdopen(fd_code, "w") as f:
            f.write(code)

        wrapper = _build_eval_wrapper(path_code, use_temp_file=True)
        return _process_emacsclient_result(_run_emacsclient(wrapper, timeout_s=60))
    finally:
        try:
            os.unlink(path_code)
        except OSError:
            pass


# ---------- MCP protocol ----------


def _handle(msg: dict) -> dict | None:
    method = msg.get("method")
    mid = msg.get("id")
    has_id = "id" in msg

    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if not has_id:
            return None
        return {
            "jsonrpc": "2.0",
            "id": (
                mid
                if isinstance(mid, (str, int)) and not isinstance(mid, bool)
                else None
            ),
            "error": {
                "code": -32600,
                "message": "Invalid request: jsonrpc must be '2.0' and method must be a string",
            },
        }

    if not has_id:
        return None

    if isinstance(mid, bool) or not isinstance(mid, (str, int)):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32600,
                "message": "Invalid request: id must be a string or integer",
            },
        }

    if method == "notifications/initialized":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {
                "code": -32600,
                "message": "Invalid request: notifications/initialized is a notification and must not include id",
            },
        }

    params = msg.get("params", {})
    if not isinstance(params, dict):
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {
                "code": -32600,
                "message": "Invalid request: params must be an object",
            },
        }

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "emacs-elisp", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": [TOOL]}}

    if method == "tools/call":
        tool = params.get("name", "")
        args = params.get("arguments", {})
        if tool not in {"emacs-elisp-eval", "elisp-eval"}:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {tool}",
                },
            }
        if not isinstance(args, dict):
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32602,
                    "message": "Invalid params: arguments must be an object",
                },
            }
        code = args.get("code")
        if not isinstance(code, str):
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32602,
                    "message": "Invalid params: code must be a string",
                },
            }
        return {"jsonrpc": "2.0", "id": mid, "result": _eval_elisp(code)}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


def _write_response(resp: dict) -> None:
    line = json.dumps(resp, separators=(",", ":")) + "\n"
    sys.stdout.write(line)
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _write_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32700,
                        "message": "Parse error",
                    },
                }
            )
            continue
        if not isinstance(msg, dict):
            _write_response(
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "Invalid request: expected JSON object",
                    },
                }
            )
            continue
        try:
            resp = _handle(msg)
        except Exception:
            mid = msg.get("id")
            if not isinstance(mid, bool) and isinstance(mid, (str, int)):
                resp = {
                    "jsonrpc": "2.0",
                    "id": mid,
                    "error": {"code": -32603, "message": "Internal error"},
                }
            else:
                continue
        if resp is not None:
            _write_response(resp)


if __name__ == "__main__":
    main()
