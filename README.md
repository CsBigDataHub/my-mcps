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
- `splunk-server-info`

What they do:

- `splunk-search`: runs an SPL query and returns formatted results
- `splunk-indexes`: lists indexes with event counts and sizes
- `splunk-search-metadata`: shows top hosts, sources, and sourcetypes for an index
- `splunk-server-info`: returns the active MCP server runtime configuration, including the selected Splunk host

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
- a target Splunk host provided either by environment variable or CLI argument

Environment variable example:

```bash
export SPLUNK_HOST="myorg.splunkcloud.com"
```

CLI argument example:

```bash
python3 /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py \
  --host myorg.splunkcloud.com \
  --server-name splunk
```

Important notes:

- `splunk-mcp.py` reads and decrypts Edge cookies from your local profile
- it is intentionally tied to the logged-in Edge session for the selected Splunk host
- if Edge is logged out, or the cookies do not exist for that host, the server will fail to authenticate
- if you register multiple MCP servers that use the same script, prefer `--host` over env-only configuration so each instance is explicitly pinned to its own host

## Run directly

Both scripts are stdio MCP servers. They do not start an HTTP listener.

Examples:

```bash
python3 /Users/ckoneru/GitRepos/myprojects/mybbscripts/elisp-eval-server.py
```

Environment variable example:

```bash
SPLUNK_HOST="myorg.splunkcloud.com" \
python3 /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py
```

Argument example:

```bash
python3 /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py \
  --host myorg.splunkcloud.com \
  --server-name splunk
```

## Configure In Claude Code

Claude Code supports project-scoped `.mcp.json` files and CLI-based MCP registration.

### Option 1: project `.mcp.json`

Create `.mcp.json` in the repo root.

Example using environment variables:

```json
{
  "mcpServers": {
    "emacs-elisp": {
      "type": "stdio",
      "command": "python3",
      "args": [
        "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/elisp-eval-server.py"
      ],
      "env": {}
    },
    "splunk": {
      "type": "stdio",
      "command": "python3",
      "args": [
        "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"
      ],
      "env": {
        "SPLUNK_HOST": "myorg.splunkcloud.com"
      }
    }
  }
}
```

Example using explicit arguments (recommended when configuring multiple Splunk servers, such as prod and nonprod):

```json
{
  "mcpServers": {
    "mcpSplunk-prod": {
      "type": "stdio",
      "command": "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py",
      "args": [
        "--host",
        "est-sh.prod.cloud-splunk-optum.com",
        "--server-name",
        "splunk-prod"
      ]
    },
    "mcpSplunk-nonprod": {
      "type": "stdio",
      "command": "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py",
      "args": [
        "--host",
        "est-sh.stage.aws-splunk-optum.com",
        "--server-name",
        "splunk-nonprod"
      ]
    }
  }
}
```

Recommended server names:

- `emacs-elisp`
- `splunk`

These are short and obvious, which helps agents choose the right server.

### Option 2: Claude CLI

Add the servers with `claude mcp add-json`.

Environment variable example:

```bash
claude mcp add-json emacs-elisp \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/elisp-eval-server.py"],"env":{}}'
```

```bash
claude mcp add-json splunk \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"],"env":{"SPLUNK_HOST":"myorg.splunkcloud.com"}}'
```

Explicit argument example:

```bash
claude mcp add-json mcpSplunk-prod \
  '{"type":"stdio","command":"/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py","args":["--host","est-sh.prod.cloud-splunk-optum.com","--server-name","splunk-prod"]}'
```

```bash
claude mcp add-json mcpSplunk-nonprod \
  '{"type":"stdio","command":"/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py","args":["--host","est-sh.stage.aws-splunk-optum.com","--server-name","splunk-nonprod"]}'
```

If you want the config checked into the repo for team use, use project scope:

```bash
claude mcp add-json emacs-elisp --scope project \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/elisp-eval-server.py"],"env":{}}'
```

```bash
claude mcp add-json splunk --scope project \
  '{"type":"stdio","command":"python3","args":["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"],"env":{"SPLUNK_HOST":"myorg.splunkcloud.com"}}'
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
  python3 /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/elisp-eval-server.py
```

Add `splunk-mcp.py` with an environment variable:

```bash
codex mcp add splunk \
  --env SPLUNK_HOST=myorg.splunkcloud.com \
  -- python3 /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py
```

Add `splunk-mcp.py` with explicit arguments:

```bash
codex mcp add mcpSplunk-nonprod -- \
  /Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py \
  --host est-sh.stage.aws-splunk-optum.com \
  --server-name splunk-nonprod
```

Verify:

```bash
codex mcp list
```

### Option 2: `~/.codex/config.toml`

Environment variable example:

```toml
[mcp_servers.emacs-elisp]
command = "python3"
args = ["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/elisp-eval-server.py"]

[mcp_servers.splunk]
command = "python3"
args = ["/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"]
env = { SPLUNK_HOST = "myorg.splunkcloud.com" }
```

Explicit argument example:

```toml
[mcp_servers.mcpSplunkProd]
command = "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"
args = ["--host", "est-sh.prod.cloud-splunk-optum.com", "--server-name", "splunk-prod"]

[mcp_servers.mcpSplunkNonprod]
command = "/Users/ckoneru/GitRepos/team-loki/ocdp-team-loki-mcp-scripts/splunk-mcp.py"
args = ["--host", "est-sh.stage.aws-splunk-optum.com", "--server-name", "splunk-nonprod"]
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
  - `splunk-server-info`
- credentials are refreshed from Edge cookies when needed
- long-running searches support cancellation via MCP notifications
- use `splunk-server-info` to confirm which host a specific MCP server instance is actually using

Example prompts:

- `Use splunk-search to find error events for the last 4 hours`
- `Use splunk-indexes to list indexes`
- `Use splunk-search-metadata for index=main`
- `Use splunk-server-info to show the active server host`

## Troubleshooting

### `elisp-eval-server.py`

If it fails:

- make sure Emacs is running with a server enabled
- verify the `emacsclient` path exists
- run `emacsclient --eval '(+ 1 1)'` manually to confirm connectivity

### `splunk-mcp.py`

If it fails:

- confirm the selected Splunk host exactly matches the Splunk cookie host in Edge
- if you configured the server with `env`, verify `SPLUNK_HOST`
- if you configured the server with args, verify `--host`
- use `splunk-server-info` to confirm which host the running MCP server instance is actually using
- confirm you are logged into Splunk in Edge
- confirm the Edge profile path exists:

```text
~/Library/Application Support/Microsoft Edge/Default/Cookies
```

- confirm macOS keychain access works:

```bash
security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w
```

### Avoiding keychain popups (automation)

The `splunk-mcp.py` script supports multiple credential storage methods to avoid interactive keychain prompts.

**Quick setup with helper script (recommended)**

Run the interactive setup script to extract and store your password once:

```bash
./setup-edge-password.sh
```

The script will prompt you once for keychain access, then let you choose:
1. Environment variable (add to `~/.bashrc` or `~/.zshrc`)
2. Plain text file with chmod 600 (recommended for most users)
3. GPG encrypted file (most secure)

**Manual setup options**

**Method 1: Environment variable (recommended for automation)**

```bash
# Extract password once (will prompt)
PASSWORD=$(security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w)

# Add to your shell profile (~/.bashrc or ~/.zshrc)
export EDGE_SAFE_STORAGE_PASSWORD='your-password-here'
```

**Method 2: Plain text file (secure with file permissions)**

```bash
# Extract and save password
mkdir -p ~/.splunk-mcp
security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w > ~/.splunk-mcp/edge-password
chmod 600 ~/.splunk-mcp/edge-password
```

**Method 3: GPG encrypted file (most secure)**

```bash
# Extract and encrypt password
mkdir -p ~/.splunk-mcp
security find-generic-password -s "Microsoft Edge Safe Storage" -a "Microsoft Edge" -w | \
  gpg --encrypt --recipient your-email@example.com > ~/.splunk-mcp/edge-password.gpg
```

**Method 4: Allow keychain prompts (not recommended for automation)**

```bash
export ALLOW_KEYCHAIN_PROMPT=1
```

**Credential lookup order**

The script tries methods in this order:
1. `EDGE_SAFE_STORAGE_PASSWORD` environment variable
2. `~/.splunk-mcp/edge-password` file (must be chmod 600)
3. `~/.splunk-mcp/edge-password.gpg` file (decrypts with gpg)
4. macOS Keychain (only if `ALLOW_KEYCHAIN_PROMPT=1` is set)

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
