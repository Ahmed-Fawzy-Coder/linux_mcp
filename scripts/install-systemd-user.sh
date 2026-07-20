#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
python_bin="${PYTHON_BIN:-python3}"

if ! command -v systemctl >/dev/null 2>&1; then
  echo "systemd is required for this installer." >&2
  exit 1
fi

if [[ ! -x "$repo_root/.venv/bin/python" ]]; then
  "$python_bin" -m venv "$repo_root/.venv"
fi
"$repo_root/.venv/bin/python" -m pip install --upgrade pip
"$repo_root/.venv/bin/python" -m pip install -e "$repo_root"

if [[ ! -f "$repo_root/mcp_server/.env" ]]; then
  cp "$repo_root/mcp_server/.env.example" "$repo_root/mcp_server/.env"
  echo "Created mcp_server/.env. Replace MCP_API_KEY before exposing the server." >&2
fi

mkdir -p "$unit_dir"

cat >"$unit_dir/linux-mcp.socket" <<EOF
[Unit]
Description=On-demand socket for the token-bounded Linux MCP server

[Socket]
ListenStream=127.0.0.1:8000
NoDelay=true

[Install]
WantedBy=sockets.target
EOF

cat >"$unit_dir/linux-mcp.service" <<EOF
[Unit]
Description=Token-bounded Linux MCP server for Codex
Requires=linux-mcp.socket
After=linux-mcp.socket

[Service]
Type=simple
WorkingDirectory=$repo_root
ExecStart="$repo_root/.venv/bin/python" -m uvicorn mcp_server.main:app --fd 3
Restart=on-failure
RestartSec=2
KillMode=control-group

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable --now linux-mcp.socket linux-mcp.service

if command -v loginctl >/dev/null 2>&1; then
  loginctl enable-linger "$USER" >/dev/null 2>&1 || true
fi

for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:8000/health >/dev/null 2>&1; then
    echo "Linux MCP is running at http://127.0.0.1:8000/mcp"
    exit 0
  fi
  sleep 0.25
done

echo "Linux MCP did not become healthy. Check: journalctl --user -u linux-mcp.service -n 100" >&2
exit 1
