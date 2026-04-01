# MCP Scripts

This repo contains two local MCP servers:

- `elisp-eval-server.py`: evaluates Emacs Lisp in a running Emacs server through `emacsclient`
- `splunk-mcp.py`: queries Splunk by borrowing Microsoft Edge session cookies on macOS

## Exposed tools

### `elisp-eval-server.py`

Primary tool:

- `emacs-elisp-eval`

Compatibility alias still accepted by `tools/call`:

- `elisp-eval`

What it does:

- evaluates Emacs Lisp in the current Emacs server
- returns the last expression result
- includes any new `*Messages*` output generated during evaluation

### `splunk-mcp.py`

Tools:

- `splunk-search`
- `splunk-indexes`
- `splunk-search-metadata`

What they do:

- `splunk-search`: runs an SPL query and returns formatted results
- `splunk-indexes`: lists indexes with event counts and sizes
- `splunk-search-metadata`: shows top hosts, sources, and sourcetypes for an index

## Requirements

### Common

- Python `3.10+`
- an MCP client that supports stdio servers

### For `elisp-eval-server.py`

- macOS
- Emacs installed
- running Emacs server
- `emacsclient` available at:

```text
/Applications/Emacs.app/Contents/MacOS/bin/emacsclient
```

### For `splunk-mcp.py`

- macOS
- Microsoft Edge installed
- active Splunk web session in Edge for the target Splunk host
- `security` CLI available from macOS
- `openssl` available in `PATH`
- `SPLUNK_HOST` set to your Splunk host, for example:

```bash
export SPLUNK_HOST="myorg.splunkcloud.com"
```

Important notes:

- `splunk-mcp.py` reads and decrypts Edge cookies from your local profile
- it is intentionally tied to the logged-in Edge session for `SPLUNK_HOST`
- if Edge is logged out, or the cookies do not exist for that host, the server will fail to authenticate

## Run directly

Both scripts are stdio MCP servers. They do not start an HTTP listener.

Examples:

```bash
python3 /Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py
```

```bash
SPLUNK_HOST="myorg.splunkcloud.com" \
python3 /Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py
```

## Configure In Claude Code

Claude Code supports project-scoped `.mcp.json` files and CLI-based MCP registration.

### Option 1: project `.mcp.json`

Create `.mcp.json` in the repo root:

```json
{
  "mcpServers": {
    "emacs-elisp": {
      "type": "stdio",
      "command": "python3",
      "args": [
        "/Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py"
      ],
      "env": {}
    },
    "splunk": {
      "type": "stdio",
      "command": "python3",
      "args": [
        "/Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py"
      ],
      "env": {
        "SPLUNK_HOST": "myorg.splunkcloud.com"
      }
    }
  }
}
```

Recommended server names:

- `emacs-elisp`
- `splunk`

These are short and obvious, which helps agents choose the right server.

### Option 2: Claude CLI

Add the servers with `claude mcp add-json`:

```bash
claude mcp add-json emacs-elisp \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py"],"env":{}}'
```

```bash
claude mcp add-json splunk \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py"],"env":{"SPLUNK_HOST":"myorg.splunkcloud.com"}}'
```

If you want the config checked into the repo for team use, use project scope:

```bash
claude mcp add-json emacs-elisp --scope project \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py"],"env":{}}'
```

```bash
claude mcp add-json splunk --scope project \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py"],"env":{"SPLUNK_HOST":"myorg.splunkcloud.com"}}'
```

Verify:

```bash
claude mcp get emacs-elisp
claude mcp get splunk
```

## Configure In OpenAI Codex

Codex supports MCP registration through the CLI and through `~/.codex/config.toml`.

### Option 1: Codex CLI

Add `elisp-eval-server.py`:

```bash
codex mcp add emacs-elisp -- \
  python3 /Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py
```

Add `splunk-mcp.py`:

```bash
codex mcp add splunk \
  --env SPLUNK_HOST=myorg.splunkcloud.com \
  -- python3 /Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py
```

Verify:

```bash
codex mcp list
```

### Option 2: `~/.codex/config.toml`

Add entries like this:

```toml
[mcp_servers.emacs-elisp]
command = "python3"
args = ["/Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py"]

[mcp_servers.splunk]
command = "python3"
args = ["/Users/ckoneru/GitRepos/myprojects/mybbscripts/splunk-mcp.py"]
env = { SPLUNK_HOST = "myorg.splunkcloud.com" }
```

Recommended names:

- `emacs-elisp`
- `splunk`

Those names are clearer than the raw script filenames and make tool selection easier for agents.

## Usage notes

### Emacs Lisp server

- tool name exposed to the agent: `emacs-elisp-eval`
- evaluates forms from a temporary file to avoid shell escaping issues
- state persists in the running Emacs session between calls

Example prompts:

- `Use emacs-elisp-eval to evaluate (+ 1 2 3)`
- `Use emacs-elisp-eval to read the *Messages* buffer`

### Splunk server

- tool names exposed to the agent:
  - `splunk-search`
  - `splunk-indexes`
  - `splunk-search-metadata`
- credentials are refreshed from Edge cookies when needed
- long-running searches support cancellation via MCP notifications

Example prompts:

- `Use splunk-search to find error events for the last 4 hours`
- `Use splunk-indexes to list indexes`
- `Use splunk-search-metadata for index=main`

## Troubleshooting

### `elisp-eval-server.py`

If it fails:

- make sure Emacs is running with a server enabled
- verify the `emacsclient` path exists
- run `emacsclient --eval '(+ 1 1)'` manually to confirm connectivity

### `splunk-mcp.py`

If it fails:

- confirm `SPLUNK_HOST` exactly matches the Splunk cookie host in Edge
- confirm you are logged into Splunk in Edge
- confirm the Edge profile path exists:

```text
~/Library/Application Support/Microsoft Edge/Default/Cookies
```

- confirm macOS keychain access works:

```bash
security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w
```

## Developer notes

### Test commands

Run a fast syntax check first:

```bash
python3 -m py_compile elisp-eval-server.py splunk-mcp.py
```

Run the protocol suites like this:

```bash
python3 -m unittest test_elisp_eval_protocol -v
```

```bash
SPLUNK_HOST=example.com python3 -m unittest test_splunk_mcp_protocol -v
```

### Maintenance rules

- keep tool names explicit and stable because MCP clients select tools by name
- preserve stdio transport behavior: one JSON-RPC message per line on stdout
- do not write logs or debug output to stdout
- prefer returning MCP tool errors in structured `content` responses instead of crashing the server

### `elisp-eval-server.py`

- primary tool name is `emacs-elisp-eval`
- legacy alias `elisp-eval` is still accepted for compatibility
- `serverInfo.name` is `emacs-elisp`
- the script assumes `emacsclient` lives at `/Applications/Emacs.app/Contents/MacOS/bin/emacsclient`

### `splunk-mcp.py`

- tool names are:
  - `splunk-search`
  - `splunk-indexes`
  - `splunk-search-metadata`
- `splunk-mcp.py` is intentionally macOS-specific because it reads Microsoft Edge cookies and macOS Keychain data
- stdout writes are synchronized and tool calls run through a bounded executor; preserve that behavior if you change concurrency
- keep JSON schemas aligned with actual accepted argument types

### Integration guidance

When changing behavior, validate these cases manually in addition to unit tests:

- `tools/list` returns the expected tool names
- `tools/call` works with a real MCP client
- `elisp-eval-server.py` can reach the running Emacs server
- `splunk-mcp.py` can refresh credentials from Edge and execute a real Splunk query

## References

The client configuration examples in this README were aligned to current official CLI/docs patterns for:

- Claude Code MCP configuration
- OpenAI Codex MCP configuration
