#!/usr/bin/env bash
# One-step installer for the Jira Agent (macOS and Linux).
#
#   curl -fsSL https://raw.githubusercontent.com/Zeeshan138063/jira-agent/main/install/install.sh | bash -s -- --site https://yoursite.atlassian.net --projects KEY1,KEY2
#
# Safe to run again: it skips what is already installed and updates the plugin.
set -euo pipefail

REPO="Zeeshan138063/jira-agent"
MARKETPLACE="team-plugins"
PLUGIN="jira-agent@${MARKETPLACE}"
SITE="${JIRA_SITE:-}"
PROJECTS="${JIRA_PROJECTS:-}"

while [ $# -gt 0 ]; do
  case "$1" in
    --site) SITE="$2"; shift 2 ;;
    --projects) PROJECTS="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
ok()   { printf '    \033[1;32mOK\033[0m %s\n' "$1"; }
fail() { printf '\n\033[1;31mSomething went wrong:\033[0m %s\n%s\n' "$1" "Send a screenshot of this window to your admin."; exit 1; }

export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
OS="$(uname -s)"

say "Step 1 of 5: Claude Code"
if command -v claude >/dev/null 2>&1; then
  ok "already installed"
else
  curl -fsSL https://claude.ai/install.sh | bash || fail "could not install Claude Code"
  command -v claude >/dev/null 2>&1 || fail "Claude Code installed but not found on PATH"
  ok "installed"
fi

say "Step 2 of 5: uv (runs the agent's Python code)"
if command -v uv >/dev/null 2>&1; then
  ok "already installed"
else
  curl -LsSf https://astral.sh/uv/install.sh | sh >/dev/null || fail "could not install uv"
  command -v uv >/dev/null 2>&1 || fail "uv installed but not found on PATH"
  ok "installed"
fi

say "Step 3 of 5: Jira Agent plugin"
claude plugin marketplace add "$REPO" >/dev/null 2>&1 || claude plugin marketplace update "$MARKETPLACE" >/dev/null 2>&1 || true
if claude plugin list 2>/dev/null | grep -q "$PLUGIN"; then
  claude plugin update "$PLUGIN" >/dev/null 2>&1 || true
  ok "already installed (updated to latest)"
else
  claude plugin install "$PLUGIN" || fail "could not install the plugin"
  ok "installed"
fi

say "Step 4 of 5: settings"
mkdir -p "$HOME/.jira-agent" "$HOME/.claude" "$HOME/jira-work"
if [ -n "$SITE" ] && [ -n "$PROJECTS" ]; then
  SITE="$SITE" PROJECTS="$PROJECTS" uv run --quiet --no-project python - <<'PY'
import json, os, pathlib
site = os.environ["SITE"].rstrip("/")
projects = [p.strip().upper() for p in os.environ["PROJECTS"].split(",") if p.strip()]
name = site.split("://", 1)[-1].split(".", 1)[0].lower()
preset = {"preset_version": 1, "suggested_name": name, "site_url": site, "projects": projects}
pathlib.Path.home().joinpath(".jira-agent", "preset.json").write_text(json.dumps(preset, indent=2))
PY
  ok "team Jira site saved ($SITE, projects $PROJECTS)"
else
  ok "no --site given: the agent will ask for the Jira site on first run"
fi
uv run --quiet --no-project python - <<'PY'
import json, pathlib
path = pathlib.Path.home() / ".claude" / "settings.json"
data = json.loads(path.read_text()) if path.exists() and path.read_text().strip() else {}
allow = data.setdefault("permissions", {}).setdefault("allow", [])
for rule in ("Bash(jira-agent:*)", "Bash(jira-agent *)"):
    if rule not in allow:
        allow.append(rule)
path.write_text(json.dumps(data, indent=2))
PY
ok "agent commands allowed (no permission pop-up on every step)"
if [ "$OS" = "Linux" ] && [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  grep -q JIRA_AGENT_SECRETS "$HOME/.profile" 2>/dev/null || echo 'export JIRA_AGENT_SECRETS=file' >> "$HOME/.profile"
  ok "no desktop keyring found: token will be kept in a private file instead"
fi

say "Step 5 of 5: desktop shortcut"
DESKTOP="$HOME/Desktop"
mkdir -p "$DESKTOP"
if [ "$OS" = "Darwin" ]; then
  LAUNCHER="$DESKTOP/Jira Agent.command"
  cat > "$LAUNCHER" <<'SH'
#!/bin/zsh -l
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/jira-work"
exec claude "Let's work on Jira"
SH
  chmod +x "$LAUNCHER"
else
  RUNNER="$HOME/.jira-agent/start.sh"
  cat > "$RUNNER" <<'SH'
#!/usr/bin/env bash
[ -f "$HOME/.profile" ] && . "$HOME/.profile"
export PATH="$HOME/.local/bin:$PATH"
cd "$HOME/jira-work"
exec claude "Let's work on Jira"
SH
  chmod +x "$RUNNER"
  LAUNCHER="$DESKTOP/jira-agent.desktop"
  cat > "$LAUNCHER" <<DESK
[Desktop Entry]
Type=Application
Name=Jira Agent
Comment=Work on Jira tickets with Claude
Exec=$RUNNER
Terminal=true
Icon=utilities-terminal
DESK
  chmod +x "$LAUNCHER"
  mkdir -p "$HOME/.local/share/applications"
  cp "$LAUNCHER" "$HOME/.local/share/applications/jira-agent.desktop"
  command -v gio >/dev/null 2>&1 && gio set "$LAUNCHER" metadata::trusted true 2>/dev/null || true
fi
ok "\"Jira Agent\" shortcut is on your Desktop"

printf '\n\033[1;32mAll done.\033[0m Double-click "Jira Agent" on your Desktop to start.\n'
printf 'The first time, it asks you to sign in to Claude and then to connect Jira (about 5 minutes).\n\n'
