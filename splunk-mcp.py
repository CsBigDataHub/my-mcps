#!/usr/bin/env python3
"""MCP server for Splunk. Borrows session credentials from Microsoft Edge on macOS."""

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

if sys.version_info < (3, 10):
    raise SystemExit("splunk-mcp.py requires Python 3.10 or newer")


# ---------- Config ----------


def _load_runtime_config() -> Tuple[str, str]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host")
    parser.add_argument("--server-name")
    args, _unknown = parser.parse_known_args()

    splunk_host = args.host or os.environ.get("SPLUNK_HOST")
    if not splunk_host:
        raise SystemExit(
            "SPLUNK_HOST env var required (e.g. myorg.splunkcloud.com), or pass --host"
        )

    server_name = args.server_name or os.environ.get("MCP_SERVER_NAME") or "splunk"
    return splunk_host, server_name


SPLUNK_HOST, SERVER_NAME = _load_runtime_config()

EDGE_COOKIES_DB = (
    Path.home() / "Library/Application Support/Microsoft Edge/Default/Cookies"
)
REFRESH_INTERVAL_S = 60
POLL_INTERVAL_S = 2
SEARCH_TIMEOUT_S = 120
HTTP_TIMEOUT_S = 45
MAX_WORKERS = 4
MAX_HTTP_RETRIES = 3
RETRY_BACKOFF_BASE = 2  # seconds

# ---------- State ----------

_credentials: Optional[Dict[str, str]] = None
_last_forced_refresh: float = 0.0
_credentials_lock = threading.RLock()

# Cancellation tracking for in-flight request handlers.
_pending_requests: Dict[Any, threading.Event] = {}
_pending_lock = threading.Lock()
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="splunk-mcp")
_write_lock = threading.Lock()

# ---------- TLS context (reused) ----------

_ssl_ctx = ssl.create_default_context()

# ---------- Cookie decryption (Chromium v10) ----------


def _keychain_password() -> str:
    """
    Retrieve Edge Safe Storage password with multiple methods (avoiding keychain popup):
    1. Environment variable: EDGE_SAFE_STORAGE_PASSWORD
    2. Plain text file: ~/.splunk-mcp/edge-password (chmod 600)
    3. GPG encrypted file: ~/.splunk-mcp/edge-password.gpg
    4. macOS Keychain (SKIPPED by default to avoid popup - set ALLOW_KEYCHAIN_PROMPT=1 to enable)
    """
    # Method 1: Environment variable (best for automation/CI)
    env_password = os.environ.get("EDGE_SAFE_STORAGE_PASSWORD")
    if env_password:
        return env_password

    # Method 2: Plain text file (must be chmod 600 for security)
    password_file = Path.home() / ".splunk-mcp" / "edge-password"
    if password_file.exists():
        stat_info = password_file.stat()
        if stat_info.st_mode & 0o077:  # Check if group/other have any permissions
            raise RuntimeError(
                f"Insecure permissions on {password_file}. Run: chmod 600 {password_file}"
            )
        return password_file.read_text().strip()

    # Method 3: GPG encrypted file
    gpg_file = Path.home() / ".splunk-mcp" / "edge-password.gpg"
    if gpg_file.exists():
        try:
            result = subprocess.run(
                ["gpg", "--decrypt", "--quiet", str(gpg_file)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass  # gpg not available or timed out

    # Method 4: macOS Keychain (ONLY if explicitly allowed to avoid popup)
    if os.environ.get("ALLOW_KEYCHAIN_PROMPT") == "1":
        try:
            r = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "Microsoft Edge Safe Storage",
                    "-a",
                    "Microsoft Edge",
                    "-w",
                ],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            if r.returncode == 0:
                return r.stdout.strip()
        except subprocess.TimeoutExpired:
            pass

    # All methods failed
    raise RuntimeError(
        "Could not retrieve Microsoft Edge keychain password. No popup-free methods found.\n\n"
        "Setup instructions (choose one):\n\n"
        "1. Environment variable (recommended for automation):\n"
        "   export EDGE_SAFE_STORAGE_PASSWORD='your-password-here'\n\n"
        "2. Plain text file (secure with file permissions):\n"
        "   mkdir -p ~/.splunk-mcp\n"
        "   echo 'your-password-here' > ~/.splunk-mcp/edge-password\n"
        "   chmod 600 ~/.splunk-mcp/edge-password\n\n"
        "3. GPG encrypted file (most secure):\n"
        "   mkdir -p ~/.splunk-mcp\n"
        "   echo 'your-password-here' | gpg --encrypt --recipient your-email@example.com > ~/.splunk-mcp/edge-password.gpg\n\n"
        "4. Allow keychain popup (not recommended for automation):\n"
        "   export ALLOW_KEYCHAIN_PROMPT=1\n\n"
        "To get your password from keychain once:\n"
        "   security find-generic-password -s 'Microsoft Edge Safe Storage' -a 'Microsoft Edge' -w\n"
    )


def _derive_key(password: str) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password.encode(), b"saltysalt", 1003, dklen=16)


def _copy_cookies_db() -> Path:
    """Atomically snapshot the Cookies DB via SQLite backup API."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="edge-cookies-"))
    dst = tmp_dir / "Cookies"
    src = sqlite3.connect(f"file:{EDGE_COOKIES_DB}?mode=ro", uri=True)
    try:
        bak = sqlite3.connect(str(dst))
        try:
            src.backup(bak)
        finally:
            bak.close()
    finally:
        src.close()
    return dst


def _query_cookie(db_path: Path, name: str, host_key: str) -> Optional[bytes]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT encrypted_value FROM cookies WHERE name=? AND host_key=? LIMIT 1",
            (name, host_key),
        ).fetchone()
        return row[0] if row else None
    finally:
        con.close()


def _decrypt_v10(ciphertext_with_prefix: bytes, key: bytes) -> Optional[str]:
    """Decrypt Chromium v10 cookie. ciphertext starts after 'v10' 3-byte prefix."""
    ciphertext = ciphertext_with_prefix[3:]
    if not ciphertext:
        return None
    iv = b"\x20" * 16
    # Use openssl for AES-128-CBC since stdlib lacks it
    proc = subprocess.run(
        [
            "openssl",
            "enc",
            "-aes-128-cbc",
            "-d",
            "-K",
            key.hex(),
            "-iv",
            iv.hex(),
            "-nopad",
        ],
        input=ciphertext,
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"openssl decryption failed (rc={proc.returncode}): {stderr or '(no stderr)'}"
        )
    plaintext = proc.stdout
    if len(plaintext) <= 32:
        raise RuntimeError(
            f"openssl produced only {len(plaintext)} bytes; expected >32 for v10 cookie"
        )
    # Skip 32-byte domain hash prefix, strip PKCS7 padding
    payload = plaintext[32:]
    pad_len = payload[-1]
    if 0 < pad_len <= 16 and payload[-pad_len:] == bytes([pad_len]) * pad_len:
        payload = payload[:-pad_len]
    result = payload.decode("utf-8", errors="replace")
    return result or None


def _get_cookie(db_path: Path, name: str, host_key: str, key: bytes) -> Optional[str]:
    encrypted = _query_cookie(db_path, name, host_key)
    if not encrypted:
        return None
    return _decrypt_v10(encrypted, key)


def _discover_port(db_path: Path) -> Optional[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT name FROM cookies WHERE name LIKE 'splunkd_%' AND host_key=? LIMIT 1",
            (SPLUNK_HOST,),
        ).fetchone()
        if not row:
            return None
        m = re.search(r"splunkd_(\d+)", row[0])
        return m.group(1) if m else None
    finally:
        con.close()


# ---------- Credential management ----------


def _load_credentials_locked() -> Dict[str, str]:
    db_path = _copy_cookies_db()
    try:
        port = _discover_port(db_path)
        if not port:
            raise RuntimeError(f"No Splunk cookies in Edge for {SPLUNK_HOST}")

        key = _derive_key(_keychain_password())

        session = _get_cookie(db_path, f"splunkd_{port}", SPLUNK_HOST, key)
        if not session:
            raise RuntimeError("Could not decrypt splunkd session cookie")

        csrf = _get_cookie(db_path, f"splunkweb_csrf_token_{port}", SPLUNK_HOST, key)
        if not csrf:
            raise RuntimeError("Could not decrypt CSRF token")
    finally:
        shutil.rmtree(db_path.parent, ignore_errors=True)

    return {"session": session, "csrf": csrf, "port": port}


def _ensure_credentials(force: bool = False) -> Dict[str, str]:
    global _credentials
    with _credentials_lock:
        if _credentials and not force:
            return _credentials
        _credentials = _load_credentials_locked()
        return _credentials


# ---------- HTTP helpers ----------


def _cookie_header(creds: Dict[str, str]) -> str:
    p = creds["port"]
    return f"splunkd_{p}={creds['session']}; splunkweb_csrf_token_{p}={creds['csrf']}"


def _request(
    method: str,
    path: str,
    headers: Dict[str, str],
    body: Optional[bytes] = None,
    timeout: int = HTTP_TIMEOUT_S,
    retry_count: int = 0,
) -> Tuple[int, Any]:
    """Make HTTP request with automatic retry for transient errors."""
    last_error = None

    for attempt in range(MAX_HTTP_RETRIES):
        try:
            conn = http.client.HTTPSConnection(
                SPLUNK_HOST, context=_ssl_ctx, timeout=timeout
            )
            try:
                conn.request(method, path, body=body, headers=headers)
                resp = conn.getresponse()
                status = resp.status
                raw = resp.read().decode("utf-8", errors="replace")

                # Retry on 502/503/504 (transient server errors)
                if status in (502, 503, 504) and attempt < MAX_HTTP_RETRIES - 1:
                    backoff = RETRY_BACKOFF_BASE**attempt
                    time.sleep(backoff)
                    continue

                try:
                    return status, json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    return status, raw
            finally:
                conn.close()
        except (http.client.HTTPException, OSError, TimeoutError) as e:
            last_error = e
            if attempt < MAX_HTTP_RETRIES - 1:
                backoff = RETRY_BACKOFF_BASE**attempt
                time.sleep(backoff)
                continue
            # Last attempt failed
            raise RuntimeError(
                f"HTTP request failed after {MAX_HTTP_RETRIES} attempts: {e}"
            ) from e

    # Should not reach here, but handle gracefully
    if last_error:
        raise RuntimeError(f"HTTP request failed: {last_error}") from last_error
    return 500, "Unknown error"


def _session_expired(status: int, body) -> bool:
    if status in (401, 403):
        return True
    return isinstance(body, str) and bool(
        re.search(r"(?i)unauthorized|session|login", body)
    )


def _should_retry(status: int, body) -> bool:
    if not _session_expired(status, body):
        return False
    with _credentials_lock:
        last_forced_refresh = _last_forced_refresh
    return (time.monotonic() - last_forced_refresh) > REFRESH_INTERVAL_S


def _force_refresh_credentials() -> Dict[str, str]:
    global _credentials, _last_forced_refresh
    with _credentials_lock:
        _credentials = _load_credentials_locked()
        _last_forced_refresh = time.monotonic()
        return _credentials


def _splunk_get(
    api_path: str, params: Optional[Dict[str, str]] = None
) -> Tuple[int, Any]:
    creds = _ensure_credentials()
    qp = {"output_mode": "json"}
    if params:
        qp.update(params)
    qs = urllib.parse.urlencode(qp)
    full_path = f"/en-US/splunkd/__raw{api_path}?{qs}"
    headers = {"Cookie": _cookie_header(creds), "Accept": "application/json"}

    status, body = _request("GET", full_path, headers)
    if _should_retry(status, body):
        creds = _force_refresh_credentials()
        headers["Cookie"] = _cookie_header(creds)
        status, body = _request("GET", full_path, headers)
    return status, body


def _splunk_post(api_path: str, form: Dict[str, str]) -> Tuple[int, Any]:
    creds = _ensure_credentials()
    merged = {"output_mode": "json", **form}
    encoded = urllib.parse.urlencode(merged).encode()
    full_path = f"/en-US/splunkd/__raw{api_path}"
    headers = {
        "Cookie": _cookie_header(creds),
        "X-Splunk-Form-Key": creds["csrf"],
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "application/json",
    }

    status, body = _request("POST", full_path, headers, body=encoded, timeout=30)
    if _should_retry(status, body):
        creds = _force_refresh_credentials()
        headers["Cookie"] = _cookie_header(creds)
        headers["X-Splunk-Form-Key"] = creds["csrf"]
        status, body = _request("POST", full_path, headers, body=encoded, timeout=30)
    return status, body


def _splunk_delete(api_path: str) -> None:
    """Best-effort DELETE with CSRF. Retries once on auth expiry, swallows all errors."""
    try:
        creds = _ensure_credentials()
        full_path = f"/en-US/splunkd/__raw{api_path}?output_mode=json"
        headers = {
            "Cookie": _cookie_header(creds),
            "X-Splunk-Form-Key": creds["csrf"],
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
        }
        status, body = _request("DELETE", full_path, headers, timeout=10)
        if _should_retry(status, body):
            creds = _force_refresh_credentials()
            headers["Cookie"] = _cookie_header(creds)
            headers["X-Splunk-Form-Key"] = creds["csrf"]
            _request("DELETE", full_path, headers, timeout=10)
    except Exception:
        pass


# ---------- Splunk search ----------


class _Cancelled(Exception):
    """Raised when a search is cancelled via notifications/cancelled."""

    pass


def _search_async(
    spl: str,
    params: Optional[Dict[str, str]] = None,
    max_results: int = 100,
    cancel_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    # Check cancellation before any network I/O
    if cancel_event and cancel_event.is_set():
        return {"cancelled": True}

    create_params = {
        "search": spl,
        "exec_mode": "normal",
        "max_count": str(max_results),
    }
    if params:
        create_params.update({k: v for k, v in params.items() if k != "count"})

    try:
        status, body = _splunk_post("/services/search/jobs", create_params)
    except RuntimeError as e:
        return {"error": f"Failed to create search job: {e}"}

    if status != 201:
        return {"error": f"Failed to create search job (HTTP {status}): {body}"}

    sid = body["sid"] if isinstance(body, dict) else None
    if not sid:
        return {"error": f"No SID in response: {body}"}

    try:
        poll_auth_retried = False
        consecutive_errors = 0
        max_consecutive_errors = 3

        for poll_attempt in range(SEARCH_TIMEOUT_S // POLL_INTERVAL_S):
            if cancel_event and cancel_event.wait(POLL_INTERVAL_S):
                raise _Cancelled()
            if not cancel_event:
                time.sleep(POLL_INTERVAL_S)

            try:
                poll_status, job_body = _splunk_get(f"/services/search/jobs/{sid}")
                consecutive_errors = 0  # Reset on successful request
            except RuntimeError as e:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return {"error": f"Network error polling job {sid}: {e}"}
                # Wait longer before retry
                if not cancel_event or not cancel_event.wait(
                    RETRY_BACKOFF_BASE * consecutive_errors
                ):
                    continue
                raise _Cancelled()

            # Handle HTTP-level errors during polling
            # Note: _splunk_get() already retries internally,
            # so if we still see session-expired here, the retry already failed.
            if _session_expired(poll_status, job_body):
                if not poll_auth_retried:
                    poll_auth_retried = True
                    continue  # allow one more poll attempt with the creds _splunk_get refreshed
                return {"error": f"Auth failure polling job {sid} (HTTP {poll_status})"}
            if poll_status == 404:
                return {
                    "error": f"Search job {sid} not found (HTTP 404) — may have been reaped"
                }
            if poll_status >= 500:
                consecutive_errors += 1
                if consecutive_errors >= max_consecutive_errors:
                    return {
                        "error": f"Server error polling job {sid} after {consecutive_errors} attempts (HTTP {poll_status}): {job_body}"
                    }
                # Wait before retry with exponential backoff
                backoff = min(RETRY_BACKOFF_BASE**consecutive_errors, 30)
                if not cancel_event or not cancel_event.wait(backoff):
                    continue
                raise _Cancelled()

            entries = []
            state = ""
            if isinstance(job_body, dict):
                entries = job_body.get("entry", [])
                if entries:
                    state = entries[0].get("content", {}).get("dispatchState", "")

            if state == "DONE":
                try:
                    rs, rb = _splunk_get(
                        f"/services/search/jobs/{sid}/results",
                        {"count": str(max_results), "output_mode": "json"},
                    )
                except RuntimeError as e:
                    return {"error": f"Failed to fetch results: {e}"}
                if rs == 200 and isinstance(rb, dict):
                    return {"results": rb.get("results", [])}
                return {"error": f"Failed to fetch results (HTTP {rs}): {rb}"}

            if state == "FAILED":
                msgs = entries[0].get("content", {}).get("messages", "")
                return {"error": f"Search job failed: {msgs}"}

        return {
            "error": f"Search timed out after {SEARCH_TIMEOUT_S}s (polled {poll_attempt + 1} times)"
        }
    except _Cancelled:
        return {"cancelled": True}
    except Exception as e:
        return {"error": f"Unexpected error during search: {e}"}
    finally:
        _splunk_delete(f"/services/search/jobs/{sid}")


# ---------- Result formatting ----------


def _format_results(results: list, max_n: int) -> str:
    total = len(results)
    shown = results[:max_n]
    if not shown:
        return "No results found."

    seen = set()
    fields = []
    for row in shown:
        for k in row:
            if k not in seen:
                seen.add(k)
                fields.append(k)

    priority = []
    rest = []
    for f in fields:
        (priority if f in ("_time", "_raw") else rest).append(f)
    rest.sort()
    ordered = priority + rest

    parts = []
    if max_n < total:
        parts.append(f"Showing {max_n} of {total} results\n")
    for i, row in enumerate(shown):
        lines = [f"--- Result {i + 1} ---"]
        for f in ordered:
            v = row.get(f)
            if v is not None:
                lines.append(f"{f}: {v}")
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _parse_bool_arg(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "on"}:
            return True
        if normalized in {"false", "0", "no", "off", ""}:
            return False
    raise ValueError("must be a boolean")


def _parse_max_results_arg(
    value: Any, *, default: int = 100, limit: int = 10000
) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError("must be an integer")
    parsed = int(value)
    if parsed < 1:
        raise ValueError("must be >= 1")
    return min(parsed, limit)


def _parse_metadata_types_arg(value: Any) -> list[str]:
    if value is None:
        return ["hosts", "sources", "sourcetypes"]
    if not isinstance(value, str):
        raise ValueError("metadata_type must be a string")
    normalized = value.strip().lower()
    if normalized not in {"hosts", "sources", "sourcetypes"}:
        raise ValueError("metadata_type must be one of: hosts, sources, sourcetypes")
    return [normalized]


# ---------- Tool handlers ----------


def _do_search(
    args: Dict[str, Any], cancel_event: Optional[threading.Event] = None
) -> Dict[str, Any]:
    try:
        max_n = _parse_max_results_arg(args.get("max_results"))
        query = args["query"].strip()
        if not query:
            raise ValueError("query must be a non-empty string")
        spl = query if query.startswith("|") else f"search {query}"
        params = {
            "earliest_time": args.get("earliest_time", "-24h"),
            "latest_time": args.get("latest_time", "now"),
        }
        result = _search_async(
            spl, params=params, max_results=max_n, cancel_event=cancel_event
        )
        if "cancelled" in result:
            return {"cancelled": True}
        if "error" in result:
            error_msg = result["error"]
            # Provide helpful context for common errors
            if "502" in error_msg or "503" in error_msg or "504" in error_msg:
                error_msg += "\n\nThis is a transient server error. The search may succeed if retried."
            elif "Network error" in error_msg:
                error_msg += (
                    "\n\nCheck your network connection and Splunk server availability."
                )
            elif "Auth failure" in error_msg:
                error_msg += "\n\nYour Splunk session may have expired. Try refreshing your browser login."
            return {
                "content": [{"type": "text", "text": f"Splunk error: {error_msg}"}],
                "isError": True,
            }
        return {
            "content": [
                {"type": "text", "text": _format_results(result["results"], max_n)}
            ]
        }
    except ValueError as e:
        return {
            "content": [{"type": "text", "text": f"Invalid argument: {e}"}],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Unexpected error: {e}"}],
            "isError": True,
        }


def _do_indexes(
    args: Dict[str, Any], cancel_event: Optional[threading.Event] = None
) -> Dict[str, Any]:
    try:
        if cancel_event and cancel_event.is_set():
            return {"cancelled": True}
        try:
            status, body = _splunk_get("/services/data/indexes", {"count": "0"})
        except RuntimeError as e:
            return {
                "content": [
                    {"type": "text", "text": f"Network error fetching indexes: {e}"}
                ],
                "isError": True,
            }
        if cancel_event and cancel_event.is_set():
            return {"cancelled": True}
        if status != 200:
            return {
                "content": [
                    {"type": "text", "text": f"Splunk error (HTTP {status}): {body}"}
                ],
                "isError": True,
            }
        include = _parse_bool_arg(args.get("include_internal"))
        entries = body.get("entry", []) if isinstance(body, dict) else []
        indexes = sorted(
            (
                {
                    "name": e["name"],
                    "events": e.get("content", {}).get("totalEventCount", "?"),
                    "size": e.get("content", {}).get("currentDBSizeMB", "?"),
                }
                for e in entries
                if include or not e["name"].startswith("_")
            ),
            key=lambda x: x["name"],
        )
        if not indexes:
            return {"content": [{"type": "text", "text": "No indexes found."}]}
        lines = [f"Indexes ({len(indexes)}):\n"]
        for ix in indexes:
            lines.append(
                f"  {ix['name']} - events: {ix['events']}, size: {ix['size']} MB"
            )
        return {"content": [{"type": "text", "text": "\n".join(lines)}]}
    except ValueError as e:
        return {
            "content": [{"type": "text", "text": f"Invalid argument: {e}"}],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Unexpected error: {e}"}],
            "isError": True,
        }


def _do_metadata(
    args: Dict[str, Any], cancel_event: Optional[threading.Event] = None
) -> Dict[str, Any]:
    try:
        index = args["index"]
        if not isinstance(index, str) or not index.strip():
            raise ValueError("index must be a non-empty string")
        types = _parse_metadata_types_arg(args.get("metadata_type"))
        sections = []
        for t in types:
            # Check cancellation between metadata queries
            if cancel_event and cancel_event.is_set():
                return {"cancelled": True}
            spl = f"| metadata type={t} index={index} | sort -totalCount | head 50"
            result = _search_async(
                spl,
                params={"earliest_time": "-7d", "latest_time": "now"},
                max_results=50,
                cancel_event=cancel_event,
            )
            if "cancelled" in result:
                return {"cancelled": True}
            header = f"== {t.upper()} (index={index}) =="
            if "error" in result:
                sections.append(f"{header}\n  (error: {result['error']})")
                continue
            entries = result.get("results", [])
            if not entries:
                sections.append(f"{header}\n  (none)")
                continue
            field = re.sub(r"s$", "", t)
            lines = [header]
            for e in entries:
                v = e.get(field, "?")
                lines.append(
                    f"  {v} - count: {e.get('totalCount', '?')}, first: {e.get('firstTime', '?')}, last: {e.get('recentTime', '?')}"
                )
            sections.append("\n".join(lines))
        return {"content": [{"type": "text", "text": "\n\n".join(sections)}]}
    except ValueError as e:
        return {
            "content": [{"type": "text", "text": f"Invalid argument: {e}"}],
            "isError": True,
        }
    except Exception as e:
        return {
            "content": [{"type": "text", "text": f"Unexpected error: {e}"}],
            "isError": True,
        }


def _do_server_info(
    args: Dict[str, Any], cancel_event: Optional[threading.Event] = None
) -> Dict[str, Any]:
    _ = (args, cancel_event)
    info = {
        "server_name": SERVER_NAME,
        "splunk_host": SPLUNK_HOST,
        "pid": os.getpid(),
        "argv": sys.argv,
    }
    return {"content": [{"type": "text", "text": json.dumps(info, indent=2)}]}


# ---------- MCP protocol ----------

TOOLS = [
    {
        "name": "splunk-search",
        "description": "Run an SPL search query against Splunk and return results. Prepends 'search' keyword automatically for non-piped queries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "SPL query string"},
                "earliest_time": {
                    "type": "string",
                    "description": "Start of time range (default: '-24h')",
                },
                "latest_time": {
                    "type": "string",
                    "description": "End of time range (default: 'now')",
                },
                "max_results": {
                    "type": "integer",
                    "description": "Max results (default: 100, max: 10000)",
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "splunk-indexes",
        "description": "List available Splunk indexes with event counts and sizes.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "include_internal": {
                    "type": "boolean",
                    "description": "Set true to include internal indexes.",
                },
            },
        },
    },
    {
        "name": "splunk-search-metadata",
        "description": "Discover hosts, sources, and sourcetypes for a Splunk index. Returns top 50 by count.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": "string",
                    "description": "Splunk index name to inspect",
                },
                "metadata_type": {
                    "type": "string",
                    "description": "Specific type: 'hosts', 'sources', or 'sourcetypes'. Omit for all three.",
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "splunk-server-info",
        "description": "Return active runtime configuration for this MCP server instance, including server name, Splunk host, PID, and argv.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]

_TOOL_DISPATCH = {
    "splunk-search": _do_search,
    "splunk-indexes": _do_indexes,
    "splunk-search-metadata": _do_metadata,
    "splunk-server-info": _do_server_info,
}


_MISSING = object()  # sentinel: "id" key absent vs. "id": null
CANCELLED_ERROR_CODE = -32800

# Methods that are JSON-RPC notifications (no "id" expected)
_NOTIFICATION_METHODS = frozenset(
    {"notifications/initialized", "notifications/cancelled"}
)


def _handle(msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    method = msg.get("method")
    mid = msg.get("id", _MISSING)
    is_notification = mid is _MISSING

    # --- Base JSON-RPC validation ---
    # Must be JSON-RPC 2.0 and method must be a string.
    if msg.get("jsonrpc") != "2.0" or not isinstance(method, str):
        if is_notification:
            return None  # can't respond without an id
        resp_id = (
            mid if isinstance(mid, (str, int)) and not isinstance(mid, bool) else None
        )
        return {
            "jsonrpc": "2.0",
            "id": resp_id,
            "error": {
                "code": -32600,
                "message": "Invalid request: jsonrpc must be '2.0' and method must be a string",
            },
        }

    # --- ID validation ---
    # JSON-RPC 2.0: id MUST be a string or integer if present.
    # Python bool is a subclass of int, so exclude it explicitly.
    if not is_notification and (
        isinstance(mid, bool) or not isinstance(mid, (str, int))
    ):
        return {
            "jsonrpc": "2.0",
            "id": None,
            "error": {
                "code": -32600,
                "message": "Invalid request: id must be a string or integer",
            },
        }

    # --- Notification handling ---
    # No "id" key means this is a notification — never respond, per MCP/JSON-RPC.
    if is_notification:
        if method == "notifications/cancelled":
            params = msg.get("params")
            if isinstance(params, dict):
                request_id = params.get("requestId")
                if isinstance(request_id, (str, int)) and not isinstance(
                    request_id, bool
                ):
                    with _pending_lock:
                        event = _pending_requests.get(request_id)
                    if event is not None:
                        event.set()
        return None

    # --- Notification method with id (protocol error) ---
    if method in _NOTIFICATION_METHODS:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {
                "code": -32600,
                "message": f"Invalid request: {method} is a notification and must not include id",
            },
        }

    # --- params validation (only for requests that will get a response) ---
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

    # --- Dispatch ---
    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}

    if method == "tools/call":
        tool = params.get("name", "")
        handler = _TOOL_DISPATCH.get(tool)
        if not handler:
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32602,
                    "message": f"Unknown tool: {tool}",
                },
            }
        args = params.get("arguments", {})
        if not isinstance(args, dict):
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": -32602,
                    "message": "Invalid params: arguments must be an object",
                },
            }
        with _pending_lock:
            cancel_event = _pending_requests.get(mid)
            if cancel_event is None:
                cancel_event = threading.Event()
                _pending_requests[mid] = cancel_event
        try:
            result = handler(args, cancel_event=cancel_event)
        finally:
            with _pending_lock:
                _pending_requests.pop(mid, None)
        if isinstance(result, dict) and result.get("cancelled"):
            return {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {
                    "code": CANCELLED_ERROR_CODE,
                    "message": "Request cancelled",
                },
            }
        return {"jsonrpc": "2.0", "id": mid, "result": result}

    if method == "ping":
        return {"jsonrpc": "2.0", "id": mid, "result": {}}

    # Unknown method with a valid id
    return {
        "jsonrpc": "2.0",
        "id": mid,
        "error": {
            "code": -32601,
            "message": f"Method not found: {method}",
        },
    }


# ---------- Main loop ----------


def _write_response(resp: Dict[str, Any]) -> None:
    line = json.dumps(resp, separators=(",", ":")) + "\n"
    with _write_lock:
        sys.stdout.write(line)
        sys.stdout.flush()


def _is_tool_call(msg: Dict[str, Any]) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("jsonrpc") == "2.0"
        and msg.get("method") == "tools/call"
        and isinstance(msg.get("id", _MISSING), (str, int))
        and not isinstance(msg.get("id", _MISSING), bool)
        and msg.get("id", _MISSING) is not _MISSING
    )


def _handle_and_respond(msg: Dict[str, Any]) -> None:
    try:
        resp = _handle(msg)
    except Exception:
        mid = msg.get("id", _MISSING)
        if isinstance(mid, (str, int)) and not isinstance(mid, bool):
            resp = {
                "jsonrpc": "2.0",
                "id": mid,
                "error": {"code": -32603, "message": "Internal error"},
            }
        else:
            return
    if resp is not None:
        _write_response(resp)


def _cancel_pending_requests() -> None:
    with _pending_lock:
        events = list(_pending_requests.values())
    for event in events:
        event.set()


def main():
    try:
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

            if _is_tool_call(msg):
                request_id = msg["id"]
                with _pending_lock:
                    _pending_requests[request_id] = threading.Event()
                _executor.submit(_handle_and_respond, msg)
                continue

            try:
                resp = _handle(msg)
            except Exception:
                mid = msg.get("id", _MISSING)
                if isinstance(mid, (str, int)) and not isinstance(mid, bool):
                    resp = {
                        "jsonrpc": "2.0",
                        "id": mid,
                        "error": {"code": -32603, "message": "Internal error"},
                    }
                else:
                    continue
            if resp is not None:
                _write_response(resp)
    finally:
        _cancel_pending_requests()
        _executor.shutdown(wait=True)


if __name__ == "__main__":
    main()
