# Mac MCP

Mac MCP is a local macOS control server for AI agents. It exposes safe, structured HTTP endpoints and MCP tools for common desktop tasks: shell commands, files, processes, background jobs, macOS automation, browser control, screenshots, search, HTTP requests, and interactive user prompts.

It is designed for two common setups:

1. MCP clients that can connect to the `/mcp` endpoint.
2. Custom GPT Actions that need an OpenAPI schema and a public HTTPS URL, usually through ngrok.

> Security note: this server can control your Mac. Do not expose it without authentication. Use a strong `MCP_API_KEY`, keep `MCP_ALLOW_NO_AUTH=false`, and only share your ngrok URL with clients you trust.

## Features

- Run zsh commands and inspect running processes.
- Start, monitor, read, and stop long-running background jobs.
- Read, write, move, copy, delete, search, and inspect files.
- Run AppleScript, open apps/URLs, use clipboard, notifications, reminders, screenshots, volume, and brightness.
- Control Safari or Google Chrome tabs, selectors, JavaScript, screenshots, scrolling, keys, coordinate clicks, and DOM snapshots.
- Ask the local user a question with a native macOS dialog during autonomous workflows.
- Use the same backend through MCP or REST endpoints for Custom GPT Actions.

## Requirements

- macOS
- Python 3.10+
- Git
- ngrok account, if you want a public HTTPS URL for Custom GPT Actions
- Optional: `brightness` CLI for brightness control

```bash
brew install python git ngrok
brew install brightness   # optional
```

## Installation

```bash
git clone https://github.com/YOUR_GITHUB_USERNAME/mac-mcp.git
cd mac-mcp
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp mcp_server/.env.example mcp_server/.env
```

Edit `mcp_server/.env`:

```env
MCP_API_KEY=replace-with-a-long-random-token
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=120
```

Generate a token with:

```bash
python3 - <<'PY'
import secrets
print(secrets.token_urlsafe(48))
PY
```

## Start, stop, restart, and status

After `pip install -e .`, the `mac-mcp` command is available inside the virtual environment:

```bash
mac-mcp start
mac-mcp status
mac-mcp restart
mac-mcp stop
```

Useful options:

```bash
mac-mcp start --host 127.0.0.1 --port 8000
mac-mcp start --reload
mac-mcp stop --force
```

Logs are written to:

```text
~/.mac-mcp/mac-mcp.log
```

You can also run the server directly:

```bash
uvicorn mcp_server.main:app --host 127.0.0.1 --port 8000
```

## Local endpoints

The server exposes:

```text
MCP:  http://127.0.0.1:8000/mcp
REST: http://127.0.0.1:8000/api/*
```

Example REST request:

```bash
curl -X POST http://127.0.0.1:8000/api/system_info \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{}'
```

Example command request:

```bash
curl -X POST http://127.0.0.1:8000/api/run \
  -H "Authorization: Bearer $MCP_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"command":"pwd && sw_vers","timeout_s":10}'
```

## Getting a static ngrok dev domain

Custom GPT Actions require a public HTTPS URL. For local development, ngrok is the easiest option.

1. Sign in or create an ngrok account.
2. Install and authenticate ngrok:

```bash
ngrok config add-authtoken YOUR_NGROK_AUTHTOKEN
```

3. Create a static domain in the ngrok dashboard:

```text
Cloud Edge / Domains -> New Domain
```

You will get a domain like:

```text
your-domain.ngrok-free.dev
```

4. Start Mac MCP locally:

```bash
mac-mcp start --host 127.0.0.1 --port 8000
```

5. Start the tunnel using your static domain:

```bash
ngrok http --domain=your-domain.ngrok-free.dev 8000
```

Your Custom GPT Actions server URL will be:

```text
https://your-domain.ngrok-free.dev
```

## Custom GPT Actions setup

Use the included OpenAPI file:

```text
openapi/custom-gpt-actions.json
```

Before importing it into the GPT builder, replace the placeholder server URL:

```json
"servers": [
  {
    "url": "https://your-static-ngrok-domain.ngrok-free.dev"
  }
]
```

with your own ngrok domain:

```json
"servers": [
  {
    "url": "https://your-domain.ngrok-free.dev"
  }
]
```

In the GPT builder:

1. Open your GPT.
2. Go to Configure -> Actions.
3. Create a new action.
4. Import `openapi/custom-gpt-actions.json`.
5. Set Authentication to API Key or Bearer token, depending on the UI.
6. Use this header format:

```text
Authorization: Bearer YOUR_MCP_API_KEY
```

The REST endpoints are all under `/api`, and the operation IDs are stable. For example:

```text
POST /api/run                 -> run_command
POST /api/system_info         -> get_system_info
POST /api/files               -> files_operation
POST /api/macos               -> macos_operation
POST /api/browser             -> browser_operation
POST /api/search              -> search_operation
POST /api/interactive         -> ask_user
```

## OpenAPI format

A Custom GPT Action schema needs three main pieces:

```json
{
  "openapi": "3.1.1",
  "info": {
    "title": "Mac MCP Server",
    "version": "1.0.0"
  },
  "servers": [
    {
      "url": "https://your-domain.ngrok-free.dev"
    }
  ],
  "paths": {
    "/api/system_info": {
      "post": {
        "operationId": "get_system_info",
        "summary": "Get macOS system information",
        "responses": {
          "200": {
            "description": "Successful response."
          }
        }
      }
    }
  }
}
```

For grouped endpoints such as `/api/files`, `/api/macos`, `/api/browser`, and `/api/search`, the `tool` field selects the internal operation. Example:

```json
{
  "tool": "read_file",
  "path": "~/Desktop/example.txt"
}
```

Browser example:

```json
{
  "tool": "browser_open_url",
  "browser": "Google Chrome",
  "url": "https://example.com",
  "new_tab": true
}
```

## MCP endpoint

Clients that support MCP over streamable HTTP can connect to:

```text
https://your-domain.ngrok-free.dev/mcp
```

Use the same bearer token if authentication is enabled.

## Security recommendations

- Keep `MCP_ALLOW_NO_AUTH=false` when using ngrok.
- Use a long random `MCP_API_KEY`.
- Prefer `127.0.0.1` for the local bind address.
- Do not commit `.env`, logs, job outputs, screenshots, or personal files.
- Review every tool you expose to AI clients. Shell, file, browser, and AppleScript tools are powerful.
- Stop the tunnel when you are not using it:

```bash
mac-mcp stop
pkill ngrok
```

## Repository structure

```text
mcp_server/                  Python server and tool implementations
openapi/custom-gpt-actions.json
pyproject.toml               Package metadata and mac-mcp CLI entry point
README.md
```

## License

MIT
