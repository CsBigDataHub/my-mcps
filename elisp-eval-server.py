#!/usr/bin/env python3
"""MCP server for elisp evaluation via emacsclient.
Writes code to a temp file and evals it - no shell escaping issues.

Original Author: Ag Ibragimov - github.com/agzam
Based on Ovi Stoica's suggestion on Clojurians.
"""

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


def _eval_elisp(code: str) -> dict:
    """Write code to a temp file, wrap it in a progn-reader, eval via emacsclient."""
    fd_code, path_code = tempfile.mkstemp(prefix="eca-elisp-", suffix=".el")
    fd_msgs, path_msgs = tempfile.mkstemp(prefix="eca-msgs-", suffix=".txt")
    try:
        # Close the msgs fd immediately — Emacs will write to it by path
        os.close(fd_msgs)

        # Write the user's elisp code to the temp file
        with os.fdopen(fd_code, "w") as f:
            f.write(code)

        # Build the wrapper elisp that:
        # 1. Records current *Messages* position
        # 2. Reads+evals all forms from the temp file
        # 3. Captures any new *Messages* output to a second temp file
        # 4. Returns the eval result
        wrapper = (
            '(let* ((msgs-buf (get-buffer-create "*Messages*"))\n'
            "       (msgs-pos (with-current-buffer msgs-buf (point-max)))\n"
            "       (result (with-temp-buffer\n"
            '                 (insert-file-contents "' + path_code + '")\n'
            "                 (goto-char (point-min))\n"
            "                 (let (forms)\n"
            "                   (condition-case nil\n"
            "                       (while t (push (read (current-buffer)) forms))\n"
            "                     (end-of-file nil))\n"
            "                   (eval (cons 'progn (nreverse forms)) t))))\n"
            "       (new-msgs (with-current-buffer msgs-buf\n"
            "                   (let ((s (string-trim (buffer-substring-no-properties msgs-pos (point-max)))))\n"
            "                     (and (not (string-empty-p s)) s)))))\n"
            "  (when new-msgs\n"
            '    (write-region new-msgs nil "' + path_msgs + "\" nil 'silent))\n"
            "  result)"
        )

        proc_result = _run_emacsclient(wrapper, timeout_s=60)
        if proc_result.get("isError"):
            return proc_result

        proc_stdout = proc_result["stdout"]
        proc_stderr = proc_result["stderr"]
        proc_returncode = proc_result["returncode"]

        # Read any captured *Messages* output
        messages = None
        try:
            with open(path_msgs, "r") as f:
                s = f.read().strip()
            if s:
                messages = s
        except (OSError, IOError):
            pass

        if proc_returncode == 0:
            content = [{"type": "text", "text": proc_stdout.strip()}]
            if messages:
                content.append(
                    {"type": "text", "text": f"--- *Messages* ---\n{messages}"}
                )
            return {"content": content}

        combined = (proc_stdout + proc_stderr).strip()
        return {"content": [{"type": "text", "text": combined}], "isError": True}

    finally:
        try:
            os.unlink(path_code)
        except OSError:
            pass
        try:
            os.unlink(path_msgs)
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
