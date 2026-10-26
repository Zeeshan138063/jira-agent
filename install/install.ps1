# One-step installer for the Jira Agent (Windows).
#
#   $env:JIRA_SITE="https://yoursite.atlassian.net"; $env:JIRA_PROJECTS="KEY1,KEY2"; irm https://raw.githubusercontent.com/Zeeshan138063/jira-agent/main/install/install.ps1 | iex
#
# Safe to run again: it skips what is already installed and updates the plugin.
$ErrorActionPreference = "Stop"

$Repo = "Zeeshan138063/jira-agent"
$Marketplace = "team-plugins"
$Plugin = "jira-agent@$Marketplace"
$Site = $env:JIRA_SITE
$Projects = $env:JIRA_PROJECTS

function Say($m)  { Write-Host "`n==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    OK $m" -ForegroundColor Green }
function Fail($m) { Write-Host "`nSomething went wrong: $m`nSend a screenshot of this window to your admin." -ForegroundColor Red; throw $m }
function Has($c)  { [bool](Get-Command $c -ErrorAction SilentlyContinue) }
# Write UTF-8 without a BOM (Windows PowerShell 5.1 adds one, which breaks JSON readers)
function Write-Utf8($path, $text) { [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false)) }

$LocalBin = Join-Path $env:USERPROFILE ".local\bin"
$env:Path = "$LocalBin;$env:Path"

Say "Step 1 of 5: Claude Code"
if (Has claude) { Ok "already installed" } else {
    try { Invoke-RestMethod https://claude.ai/install.ps1 | Invoke-Expression } catch { Fail "could not install Claude Code" }
    $env:Path = "$LocalBin;$env:Path"
    if (-not (Has claude)) { Fail "Claude Code installed but not found on PATH" }
    Ok "installed"
}
if (-not (Has git)) {
    Write-Host "    Note: Git for Windows is not installed. Claude Code on Windows needs it." -ForegroundColor Yellow
    if (Has winget) {
        winget install --id Git.Git -e --silent --accept-package-agreements --accept-source-agreements | Out-Null
        $env:Path = "$env:ProgramFiles\Git\cmd;$env:Path"
        Ok "Git installed"
    } else { Fail "please install Git from https://git-scm.com/download/win and run this again" }
}

Say "Step 2 of 5: uv (runs the agent's Python code)"
if (Has uv) { Ok "already installed" } else {
    try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression | Out-Null } catch { Fail "could not install uv" }
    $env:Path = "$LocalBin;$env:Path"
    if (-not (Has uv)) { Fail "uv installed but not found on PATH" }
    Ok "installed"
}

Say "Step 3 of 5: Jira Agent plugin"
$ErrorActionPreference = "Continue"
claude plugin marketplace add $Repo *> $null
if ($LASTEXITCODE -ne 0) { claude plugin marketplace update $Marketplace *> $null }
$installed = (claude plugin list 2>$null | Out-String) -match [regex]::Escape($Plugin)
if ($installed) {
    claude plugin update $Plugin *> $null
    Ok "already installed (updated to latest)"
} else {
    claude plugin install $Plugin
    if ($LASTEXITCODE -ne 0) { Fail "could not install the plugin" }
    Ok "installed"
}
$ErrorActionPreference = "Stop"

Say "Step 4 of 5: settings"
$AgentHome = Join-Path $env:USERPROFILE ".jira-agent"
$ClaudeHome = Join-Path $env:USERPROFILE ".claude"
$WorkDir = Join-Path $env:USERPROFILE "jira-work"
New-Item -ItemType Directory -Force -Path $AgentHome, $ClaudeHome, $WorkDir | Out-Null

if ($Site -and $Projects) {
    $siteUrl = $Site.TrimEnd("/")
    $name = ($siteUrl -replace "^https?://", "").Split(".")[0].ToLower()
    $preset = [ordered]@{
        preset_version = 1
        suggested_name = $name
        site_url       = $siteUrl
        projects       = @($Projects.Split(",") | ForEach-Object { $_.Trim().ToUpper() } | Where-Object { $_ })
    }
    Write-Utf8 (Join-Path $AgentHome "preset.json") ($preset | ConvertTo-Json)
    Ok "team Jira site saved ($siteUrl, projects $Projects)"
} else {
    Ok "no JIRA_SITE given: the agent will ask for the Jira site on first run"
}

$settingsPath = Join-Path $ClaudeHome "settings.json"
$settings = [pscustomobject]@{}
if ((Test-Path $settingsPath) -and (Get-Content $settingsPath -Raw).Trim()) {
    $settings = Get-Content $settingsPath -Raw | ConvertFrom-Json
}
if (-not $settings.PSObject.Properties["permissions"]) {
    $settings | Add-Member -NotePropertyName permissions -NotePropertyValue ([pscustomobject]@{})
}
if (-not $settings.permissions.PSObject.Properties["allow"]) {
    $settings.permissions | Add-Member -NotePropertyName allow -NotePropertyValue @()
}
$allow = [System.Collections.ArrayList]@($settings.permissions.allow)
foreach ($rule in @("Bash(jira-agent:*)", "Bash(jira-agent *)")) {
    if (-not $allow.Contains($rule)) { [void]$allow.Add($rule) }
}
$settings.permissions.allow = @($allow)
Write-Utf8 $settingsPath ($settings | ConvertTo-Json -Depth 20)
Ok "agent commands allowed (no permission pop-up on every step)"

Say "Step 5 of 5: desktop shortcut"
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desktop "Jira Agent.lnk"))
$lnk.TargetPath = "$env:WINDIR\System32\WindowsPowerShell\v1.0\powershell.exe"
$lnk.Arguments = "-NoExit -NoProfile -Command `"`$env:Path='$LocalBin;'+`$env:Path; claude 'Let''s work on Jira'`""
$lnk.WorkingDirectory = $WorkDir
$lnk.Description = "Work on Jira tickets with Claude"
$lnk.Save()
Ok "`"Jira Agent`" shortcut is on your Desktop"

Write-Host "`nAll done. Double-click `"Jira Agent`" on your Desktop to start." -ForegroundColor Green
Write-Host "The first time, it asks you to sign in to Claude and then to connect Jira (about 5 minutes).`n"
