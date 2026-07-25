# Deploy the current branch to the CasaOS box.
#
# Usage:  .\deploy.ps1                       # commit any staged changes? no - just push + rebuild
#         .\deploy.ps1 -Message "fix bug"    # commit all tracked changes with this msg, push, rebuild
#         .\deploy.ps1 -Logs                 # also tail logs after rebuild
#
# Assumes:
#   - origin remote is set (named `upstream` in this repo)
#   - SSH host alias `casaos` is configured
#   - repo is at ~/youtube-podcast-server on the box

param(
    [string]$Message = "",
    [switch]$Logs
)

$ErrorActionPreference = "Stop"

if ($Message) {
    git add -A
    git commit -m $Message
}

Write-Host "==> Pushing to GitHub..."
git push upstream master

Write-Host "==> Pulling + rebuilding on casaos..."
ssh casaos "cd ~/youtube-podcast-server && git pull --ff-only && docker compose up -d --build"

Write-Host "==> Health check..."
$health = curl -s http://casaos.local:5757/health
Write-Host $health

if ($Logs) {
    Write-Host "==> Tailing logs (Ctrl-C to stop)..."
    ssh casaos "docker logs -f youtube-podcast-server"
}
