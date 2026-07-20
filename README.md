# Linux MCP for Codex

A Linux-first fork of [bulutarkan/mac-mcp](https://github.com/bulutarkan/mac-mcp) focused on token-efficient local coding workflows in Codex.

Linux MCP runs on your machine and exposes one compact MCP gateway, `workspace`. The model searches first, reads bounded sections, edits locally, runs tests, and retrieves short log tails instead of loading whole projects or unbounded command output into the conversation.

> ملخص: هذه النسخة تشغّل MCP محليًا على Linux، تبدأ تلقائيًا مع الجهاز، وتعرض لـCodex أداة واحدة مضغوطة تقلل النص الذي يدخل إلى الـcontext. اتبع قسم Quick start ثم أضف إعداد Codex وأعد تشغيل التطبيق.

## What this fork adds

- Linux implementations for shell, files, processes, desktop helpers, search, screenshots, clipboard, browser/CDP, and background jobs.
- A single token-efficient `workspace` tool instead of loading every legacy tool schema.
- Bounded file reads: 160 lines by default, 500 maximum, and a 12,000-character response budget.
- Bounded searches: 50 results by default and 200 maximum.
- Bounded command and job output: latest 100 lines and 12,000 characters by default.
- Parallel command execution and background jobs with bounded log retrieval.
- systemd user service plus socket activation on `127.0.0.1:8000`.
- Boot-time startup with systemd user lingering.
- Live estimated savings at `/metrics`, calculated from calls made since the last telemetry reset with a bounded per-operation baseline.
- Compatibility with the companion [OpenCodex fork](https://github.com/Ahmed-Fawzy-Coder/opencodex), which displays Linux MCP statistics on its Usage page.

## Supported `workspace` actions

| Action | Purpose |
| --- | --- |
| `search_files` | Search project text with a strict result limit |
| `read_file` | Read a bounded line range |
| `read_multiple_files` | Read bounded ranges from up to eight files |
| `edit_file` | Exact find-and-replace with replacement-count validation |
| `write_file` | Write one file |
| `write_files_batch` | Write multiple files |
| `run_command` | Run a command and return bounded recent output |
| `run_commands_parallel` | Run independent commands concurrently |
| `start_background_job` | Start a long-running build, test, or server |
| `get_job_status` | Check job state |
| `get_job_output` | Read a bounded recent log section |
| `wait_jobs` | Wait for one or more jobs |
| `stop_job` | Stop a background job |

## Requirements

- A Linux distribution with Python 3.10 or newer.
- `python3-venv`, `git`, and `curl`.
- systemd for automatic startup. Manual startup also works without systemd.
- Codex Desktop or another MCP client that supports Streamable HTTP.

On Ubuntu or Debian:

```bash
sudo apt update
sudo apt install -y python3 python3-venv git curl
```

Optional desktop integrations:

```bash
sudo apt install -y ripgrep libnotify-bin wl-clipboard xclip brightnessctl
```

## Quick start

Clone your fork:

```bash
git clone https://github.com/Ahmed-Fawzy-Coder/linux_mcp.git
cd linux_mcp
```

Install the Python environment and the systemd user units:

```bash
chmod +x scripts/install-systemd-user.sh scripts/uninstall-systemd-user.sh
./scripts/install-systemd-user.sh
```

The installer:

1. Creates `.venv`.
2. Installs the project in editable mode.
3. Creates `mcp_server/.env` from the safe example if it is missing.
4. Installs and enables `linux-mcp.socket` and `linux-mcp.service`.
5. Enables user lingering when allowed, so the service can start at boot before an interactive login.
6. Verifies `http://127.0.0.1:8000/health`.

## Configure the local secret

Generate a token:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Edit `mcp_server/.env`:

```env
MCP_API_KEY=replace-with-the-generated-value
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=1000
DEFAULT_COMMAND_TIMEOUT_S=120
MAX_COMMAND_TIMEOUT_S=600
MAX_OUTPUT_CHARS=12000
```

Restart the service after changing `.env`:

```bash
systemctl --user restart linux-mcp.service
```

Do not commit `mcp_server/.env`. It is ignored by Git.

## Configure Codex globally

Open `~/.codex/config.toml` and add the following block. Replace the bearer value with the same `MCP_API_KEY` used in `mcp_server/.env`:

```toml
[mcp_servers.linux_mcp]
enabled_tools = ["workspace"]
url = "http://127.0.0.1:8000/mcp"
http_headers = { "Authorization" = "Bearer replace-with-your-MCP_API_KEY" }
enabled = true
required = true
startup_timeout_sec = 120
tool_timeout_sec = 300
default_tools_approval_mode = "approve"
```

The same block is available in [`examples/codex-config.toml`](examples/codex-config.toml).

Important details:

- Keep `enabled_tools = ["workspace"]`. Loading only the compact gateway avoids sending every legacy tool schema to the model.
- `required = true` asks Codex to wait for the MCP server during initial startup.
- `startup_timeout_sec = 120` gives socket activation and the first Python import enough time on slower machines.
- The server only listens on localhost by default.

Restart Codex Desktop completely after changing `config.toml`.

## Tell Codex to use it for every project

Add the contents of [`examples/AGENTS.md`](examples/AGENTS.md) to your global `~/.codex/AGENTS.md`. The key rules are:

```markdown
- Use `mcp__linux_mcp__workspace` first for local project search, reads, commands, tests, jobs, and logs.
- Search first, then read only bounded ranges with explicit offset and length.
- Pass the active project's absolute path, not the MCP server directory.
- Keep command and log output to the latest 100 lines and at most 12,000 characters.
- Retry once during startup before falling back to native tools.
```

These instructions matter because MCP availability alone does not force an agent to choose it.

## Verify the installation

Check both units:

```bash
systemctl --user is-enabled linux-mcp.socket linux-mcp.service
systemctl --user is-active linux-mcp.socket linux-mcp.service
loginctl show-user "$USER" -p Linger
```

Check the server:

```bash
curl http://127.0.0.1:8000/health
curl 'http://127.0.0.1:8000/metrics?range=30d'
```

Expected health response:

```json
{"ok":true,"server":"linux-mcp","workdir":"/home/you"}
```

In a new Codex task, ask:

```text
Use linux_mcp to search this project for README, then read only the first 20 lines.
```

The tool call should be `mcp__linux_mcp__workspace` with `action: "search_files"` or `action: "read_file"`.

## How live telemetry works

Linux MCP records only metadata, never file contents or command output, in `mcp_server/audit.log`.

Telemetry records and estimates:

- characters actually returned in the compact MCP payload;
- a native-equivalent baseline capped at 40,000 characters per measured operation;
- estimated characters and tokens saved relative to that capped baseline;
- a live aggregate savings percentage and calls since reset.

The 40,000-character cap is close to a 10,000-token native tool-output budget at `4 characters ≈ 1 token`. It prevents raw local output such as a 1.7GB grep result from producing a near-100% estimate. The percentage is still explicitly an estimate, but it is dynamic, starts fresh after reset, and uses no historical benchmark data.

Endpoints:

```text
GET /metrics?range=7d
GET /metrics?range=30d
GET /metrics?range=all
```

To reset only Linux MCP telemetry:

```bash
systemctl --user stop linux-mcp.service
rm -- mcp_server/audit.log
systemctl --user start linux-mcp.service
```

## Manual startup without systemd

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
cp mcp_server/.env.example mcp_server/.env
.venv/bin/linux-mcp start --host 127.0.0.1 --port 8000
```

Compatibility alias:

```bash
.venv/bin/mac-mcp status
```

The `mac-mcp` name is retained only for compatibility with the upstream project.

## Commands and logs

```bash
systemctl --user status linux-mcp.service
journalctl --user -u linux-mcp.service -n 100 --no-pager
systemctl --user restart linux-mcp.service
systemctl --user stop linux-mcp.service
systemctl --user start linux-mcp.service
```

Socket activation test:

```bash
systemctl --user stop linux-mcp.service
curl http://127.0.0.1:8000/health
systemctl --user is-active linux-mcp.service
```

The curl request should activate the service through `linux-mcp.socket`.

## Security

- Keep the listener on `127.0.0.1` unless you have a specific protected-network design.
- Use a strong `MCP_API_KEY` and keep `MCP_ALLOW_NO_AUTH=false`.
- `run_command` can execute local shell commands. Set `MCP_ALLOW_SHELL=false` if your use case does not need it.
- Never expose the server directly to the public internet.
- HTTP and browser tools have separate allowlists in `.env`.
- Audit telemetry contains tool names, durations, and sizes only; it does not store returned content.

## Tests

```bash
.venv/bin/python -m unittest -q tests.test_token_bounds
```

The tests cover bounded reads, search limits, combined command-output limits, tail preservation, explicit working directories, job log bounds, compact gateway responses, and telemetry aggregation.

## Updating

```bash
git pull --ff-only
.venv/bin/pip install -e .
systemctl --user restart linux-mcp.service
```

## Uninstalling the service

```bash
./scripts/uninstall-systemd-user.sh
```

This removes only the systemd user units. The repository, `.env`, audit log, and virtual environment remain untouched.

To remove the Codex integration, also delete `[mcp_servers.linux_mcp]` from `~/.codex/config.toml` and remove the Linux MCP section from `~/.codex/AGENTS.md`, then restart Codex.

## Troubleshooting

### Port 8000 is already in use

```bash
ss -ltnp | grep ':8000'
systemctl --user status linux-mcp.socket linux-mcp.service
```

Stop the conflicting process or change both the socket port and the URL in Codex.

### MCP does not appear in a new Codex task

1. Confirm the service and socket are active.
2. Confirm the token in Codex matches `MCP_API_KEY`.
3. Confirm `enabled_tools = ["workspace"]`.
4. Fully restart Codex Desktop.
5. Allow the configured startup grace period once; do not immediately switch to native tools.

### Unauthorized

The bearer token in `~/.codex/config.toml` does not match `MCP_API_KEY`, or the server was not restarted after changing `.env`.

### The service starts manually but not after reboot

```bash
loginctl enable-linger "$USER"
systemctl --user enable linux-mcp.socket linux-mcp.service
```

### Large output still reaches the model

Check that Codex is calling only `workspace`, not legacy tools, and confirm:

```env
MAX_OUTPUT_CHARS=12000
```

## Project layout

```text
mcp_server/main.py             MCP server and compact gateway registration
mcp_server/tools_workspace.py  Workspace action dispatcher
mcp_server/tools_files.py      Bounded file reads and edits
mcp_server/tools_search.py     Bounded content search
mcp_server/tools_terminal.py   Bounded shell execution
mcp_server/tools_jobs.py       Background jobs and bounded logs
mcp_server/telemetry.py        Local payload telemetry aggregation
scripts/                       systemd user installation helpers
examples/                      Codex global configuration examples
tests/                         token-bound and telemetry tests
```

## Credits

Based on [bulutarkan/mac-mcp](https://github.com/bulutarkan/mac-mcp). The original MIT license is preserved.
