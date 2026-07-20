#!/usr/bin/env bash
set -euo pipefail

unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
systemctl --user disable --now linux-mcp.service linux-mcp.socket 2>/dev/null || true
rm -f -- "$unit_dir/linux-mcp.service" "$unit_dir/linux-mcp.socket"
systemctl --user daemon-reload
systemctl --user reset-failed
echo "Removed the Linux MCP user services. The repository and its data were left intact."
