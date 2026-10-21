@echo off
rem Windows launcher: same as bin/jira-agent. `uv` builds the plugin's own environment on first use.
set "root=%~dp0.."
uv run --quiet --project "%root%" jira-agent %*
