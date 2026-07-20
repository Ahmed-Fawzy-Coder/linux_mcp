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
- Optional reversible Ultimate Context snapshots with deterministic action-aware reduction, bounded retrieval, TTL/size eviction, and explicit source-completeness reporting.
- systemd user service plus socket activation on `127.0.0.1:8000`.
- Boot-time startup with systemd user lingering.
- Live estimated savings at `/metrics`, calculated from calls made since the last telemetry reset with a bounded per-operation baseline.
- Compatibility with the companion [OpenCodex fork](https://github.com/Ahmed-Fawzy-Coder/opencodex), which displays Linux MCP statistics on its Usage page.

## Supported `workspace` actions

| Action | Purpose |
| --- | --- |
| `search_files` | Search project text with a strict result limit |
| `read_file` | Read a bounded line range (`offset` is zero-based) |
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
| `get_context_result` | Retrieve a bounded section of an opaque stored context snapshot |

Path arguments are action-specific: `search_files.path` is the absolute project-root directory,
`read_file.path` is the absolute full file path including its filename, and `run_command.cwd` is the
absolute project root. `read_file.offset` is zero-based. For compatibility with weaker models, the
gateway safely normalizes the common `{ "path": "/project", "file": "package.json" }` alias; the
`file` part must remain relative and cannot contain `..`, be absolute, or escape through a symlink.

`run_command` uses `tail_lines` as its canonical line bound. The compact gateway also accepts the common `max_output_lines` alias and maps it to `tail_lines`, so a harmless naming variation does not create a failed tool round trip. If both are supplied, `tail_lines` wins.

Pass `_context` inside an action's `arguments` to opt in. `mode: "auto"` atomically stores the complete bounded action response, then reduces only known large fields. `mode: "store"` stores without reducing, `mode: "full"` adds validation metadata without storing, and `mode: "off"` preserves the legacy payload. `intent` guides conservative prioritization and `if_none_match` accepts the prior SHA-256 ETag. `snapshot_complete` describes the stored bounded response; `source_complete: false` means the underlying action had already truncated its source and that omitted source was never recoverable from the snapshot.

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

## Complete startup and automatic-boot guide

There are three separate pieces to understand:

| Piece | What it does | When it runs |
| --- | --- | --- |
| `linux-mcp.socket` | Reserves `127.0.0.1:8000` and can activate the server on demand | At boot when lingering is enabled, otherwise at login |
| `linux-mcp.service` | Runs the Python MCP server | Immediately after installation and on future boots/logins |
| Codex MCP configuration | Tells every Codex project to connect to the local server | Whenever Codex starts a task |

The installation script configures the first two pieces. The sections below configure Codex itself.

### One-time automatic-start setup

Run this from the cloned repository:

```bash
./scripts/install-systemd-user.sh
```

The installer normally enables lingering automatically. Verify it explicitly:

```bash
loginctl show-user "$USER" -p Linger
```

The expected value is `Linger=yes`. If it is `no`, enable it and check again:

```bash
loginctl enable-linger "$USER"
# If your distribution requires administrator authorization:
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger
```

Why lingering matters: a systemd user service normally starts only after that user logs in. Lingering starts the user's systemd manager during computer boot, so Linux MCP is ready before Codex Desktop is opened and even before an interactive login.

Enable and start both units again if you ever changed their state manually:

```bash
systemctl --user daemon-reload
systemctl --user enable --now linux-mcp.socket linux-mcp.service
```

No root-owned system service is required. The installed files live under:

```text
~/.config/systemd/user/linux-mcp.socket
~/.config/systemd/user/linux-mcp.service
```

The service contains an absolute repository and virtual-environment path. Do not move or delete the cloned repository while the service is installed. If you move it, rerun `./scripts/install-systemd-user.sh` from the new location.

### What happens after every reboot

1. systemd starts the user's service manager because `Linger=yes`.
2. `linux-mcp.socket` listens only on `127.0.0.1:8000`.
3. `linux-mcp.service` starts the server; socket activation also recovers it on demand.
4. Codex reads the global `[mcp_servers.linux_mcp]` configuration.
5. Codex waits for the configured startup grace period and loads only the compact `workspace` tool.

You do **not** need to run `start-linux-mcp` or start a terminal after reboot when both units are enabled. A manual helper can still be used for diagnostics, but systemd is the source of truth.

### Prove automatic startup with a reboot test

Before rebooting:

```bash
systemctl --user is-enabled linux-mcp.socket linux-mcp.service
loginctl show-user "$USER" -p Linger
```

Expected output is two `enabled` lines and `Linger=yes`. Reboot, do not run a manual start command, and then check:

```bash
systemctl --user is-active linux-mcp.socket linux-mcp.service
curl --fail --silent --show-error http://127.0.0.1:8000/health
```

Expected output is two `active` lines followed by a JSON response containing `"ok": true`.

### Daily service controls

| Goal | Command |
| --- | --- |
| Show full status | `systemctl --user status linux-mcp.socket linux-mcp.service` |
| Start now | `systemctl --user start linux-mcp.socket linux-mcp.service` |
| Restart after config/code changes | `systemctl --user restart linux-mcp.service` |
| Stop the server temporarily | `systemctl --user stop linux-mcp.service` |
| Disable future automatic startup | `systemctl --user disable --now linux-mcp.service linux-mcp.socket` |
| Re-enable automatic startup | `systemctl --user enable --now linux-mcp.socket linux-mcp.service` |
| Read recent logs | `journalctl --user -u linux-mcp.service -n 100 --no-pager` |
| Follow live logs | `journalctl --user -u linux-mcp.service -f` |

Stopping only the service does not necessarily keep it stopped: a request to port `8000` can activate it again through the enabled socket. Stop the socket as well when you need a complete temporary shutdown.

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
CONTEXT_RESULTS_DIR=~/.linux-mcp/context-results
CONTEXT_RESULT_TTL_S=3600
CONTEXT_RESULT_MAX_ENTRIES=128
CONTEXT_RESULT_MAX_BYTES=67108864
CONTEXT_RESULT_MAX_RETRIEVAL_CHARS=12000
CONTEXT_RESULT_REDUCE_CHARS=4000
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

### Enforce Linux MCP globally in Codex

`AGENTS.md` is model guidance, not a technical block. Current Codex Desktop routes
tool orchestration through a tool named `exec`, so an older hook that matches only
`Bash` does not see native `tools.exec_command` calls.

Install this fork's global guard:

```bash
python3 scripts/install-codex-hook.py
```

Then restart Codex and approve the new global `PreToolUse` hook once. The guard:

- blocks native `exec_command`, shell, read, grep, and glob operations;
- directs the agent to `mcp__linux_mcp__workspace`;
- still allows `apply_patch`, browser tools, and Linux MCP recovery commands;
- blocks Linux MCP paths outside the active project when both paths are under a
  `Coding` workspace;
- blocks Docker targets prefixed with the name of another project, such as using
  `haweyat-postgres-1` from a `GapHunter` task.

The installer backs up the previous `~/.codex/hooks.json` before replacing the
obsolete `graphify hook-check` entry. Project-specific hooks remain unchanged.

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

### Verify the complete Codex integration

Use a new Codex task so it reloads the global MCP configuration. A reliable test prompt is:

```text
Use mcp__linux_mcp__workspace only. Search this project for README files, then read
the first 20 lines of the most relevant README. Do not use native shell or file tools.
```

Confirm all of the following:

1. The first local operation is `mcp__linux_mcp__workspace`.
2. The request uses the active project's absolute path, not the `linux_mcp` repository path.
3. The model calls `search_files` before a bounded `read_file`.
4. Only the requested lines are returned.
5. `curl 'http://127.0.0.1:8000/metrics?range=all'` shows the new measured calls.

If Codex still selects native tools, the server may be healthy while the global instructions are missing. Recheck both `~/.codex/config.toml` and `~/.codex/AGENTS.md`, then completely quit and reopen Codex Desktop.

For a stronger test, do not mention Linux MCP in the prompt. Start a fresh task and ask only:

```text
In this project, find the most relevant README, read only its first five lines,
and return the first heading. Do not modify any file.
```

With the global configuration from this guide, the first local operation should still be `mcp__linux_mcp__workspace`, followed by bounded `search_files` and `read_file` actions. This proves that selection comes from the global setup rather than from prompt-specific wording.

Permanent coverage for new tasks relies on all three layers being present:

1. `[mcp_servers.linux_mcp]` is globally enabled and `required = true` in `~/.codex/config.toml`.
2. The global rules in `~/.codex/AGENTS.md` require Linux MCP for local project operations.
3. The global `PreToolUse` guard in `~/.codex/hooks.json` blocks accidental native file/shell fallbacks while Linux MCP is available.

Validate the guard after installation or update:

```bash
.venv/bin/python -m unittest -q tests.test_codex_hook_guard
```

Completely restart Codex Desktop after changing the MCP configuration, global rules, or hook. Already-open tasks may retain their previous tool registry or instructions. Linux MCP is intended for local files, search, commands, tests, jobs, and logs; browser, GitHub, image, and other specialized tools continue to use their own integrations.

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

Run the reproducible ten-round Ultimate Context benchmark:

```bash
.venv/bin/python scripts/benchmark-ultimate-context.py
```

## Updating

```bash
git pull --ff-only
.venv/bin/pip install -e .
systemctl --user restart linux-mcp.service
```

If dependencies, the repository path, or the unit template changed, use the full installer instead:

```bash
git pull --ff-only
./scripts/install-systemd-user.sh
```

The installer is safe to rerun: it refreshes the editable environment, rewrites the user units with the current absolute path, enables them, restarts the service, and performs a health check. It preserves an existing `mcp_server/.env`.

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

Then inspect the boot and user-service state:

```bash
loginctl show-user "$USER" -p Linger -p State
systemctl --user list-unit-files 'linux-mcp.*'
journalctl --user -u linux-mcp.service -b -n 100 --no-pager
```

If the current distribution does not provide a systemd user manager, use the manual startup command under a process supervisor supported by that distribution.

### `systemctl --user` cannot connect to the bus

This usually happens in a minimal SSH shell, container, or environment without a user systemd session. On a normal Linux host, log in once as the target desktop user and retry. Over SSH, confirm `/run/user/$(id -u)` exists and that `loginctl show-user "$USER"` reports a user manager. Linux MCP's systemd installer is not intended to run inside Docker.

### The repository or Python location changed

The service uses absolute paths. From the repository's new location, rerun:

```bash
./scripts/install-systemd-user.sh
systemctl --user cat linux-mcp.service
```

Confirm `WorkingDirectory` and `ExecStart` point to the new clone.

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
