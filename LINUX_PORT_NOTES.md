# Linux port notes for bulutarkan/mac-mcp

This patch keeps the original `mac-mcp` command name for compatibility, but changes the runtime behavior to Linux.

## What was changed

- Terminal commands now use `$SHELL` or `/bin/bash` instead of `/bin/zsh`.
- System info now uses Linux commands: `/etc/os-release`, `uname`, `lscpu`, `free`, `df`, `upower`/`/sys/class/power_supply`, and `ip`.
- macOS desktop functions were replaced by Linux equivalents:
  - notifications: `notify-send`
  - clipboard: `wl-copy`/`wl-paste`, `xclip`, or `xsel`
  - open URL/app: `xdg-open`, `gio`, `gtk-launch`, or executable launch
  - volume: `pactl` or `amixer`
  - brightness: `brightnessctl` or `xrandr`
  - screenshots: `gnome-screenshot`, `grim`, `scrot`, or ImageMagick `import`
  - interactive prompt: `zenity` or `kdialog`
- Spotlight search was replaced by `fd`/`fdfind`, `locate`, or `find`.
- Browser automation was replaced by Chrome DevTools Protocol over `127.0.0.1:9222`.

## Recommended Ubuntu packages

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip git \
  libnotify-bin xdg-utils wl-clipboard xclip zenity \
  wmctrl xdotool brightnessctl gnome-screenshot ripgrep fd-find chromium-browser
```

On some Ubuntu versions Chromium is a snap package. Google Chrome also works.

## Install after patch

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
cp -n mcp_server/.env.example mcp_server/.env
```

Edit `mcp_server/.env` and use a strong API key:

```env
MCP_API_KEY=replace-with-a-long-random-token
MCP_ALLOW_NO_AUTH=false
MCP_ALLOW_SHELL=true
RATE_LIMIT_PER_MINUTE=120
WORKDIR=/home/YOUR_USER
```

Start:

```bash
mac-mcp start --host 127.0.0.1 --port 8000
curl http://127.0.0.1:8000/health
```

## Browser automation note

This Linux port controls a Chrome/Chromium-compatible browser through Chrome DevTools Protocol. It will start a separate browser profile at:

```text
~/.cache/mac-mcp/chrome-cdp
```

This is safer and more reliable than trying to control your existing browser windows.

## Codex MCP config example

```bash
codex mcp add linux-mcp --url http://127.0.0.1:8000/mcp --bearer-token "$MCP_API_KEY"
```

If your Codex CLI version does not accept `--url`, add it manually to `~/.codex/config.toml` according to your Codex MCP format.
