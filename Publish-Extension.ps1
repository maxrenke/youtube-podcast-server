<#
.SYNOPSIS
Signs the Firefox extension with Mozilla and publishes it, so installed copies update themselves.

.DESCRIPTION
One command per release:

  1. picks the version: the one in firefox-extension\manifest.json if it has never
     been released, otherwise that version with the last number raised by one
     (or -Version);
  2. lints the extension with Mozilla's web-ext;
  3. has addons.mozilla.org sign it as a self-distributed ("unlisted") add-on -
     nothing is listed publicly - and downloads the signed .xpi;
  4. attaches the .xpi to a GitHub release named extension-v<version>;
  5. adds the version to firefox-extension-updates.json and pushes it.

Firefox reads that file (the manifest's update_url) about once a day and installs
any newer version on its own; about:addons > gear menu > Check for Updates does it
at once.

Needs addons.mozilla.org API credentials: create them at
https://addons.mozilla.org/en-US/developers/addon/api/key/ and save the JWT issuer
and the JWT secret as two lines in ~\.amo-api-key (or set WEB_EXT_API_KEY and
WEB_EXT_API_SECRET). Also needs node (npx) and a signed-in gh.

.PARAMETER Version
Release this version instead of the automatic one, e.g. 1.2.0.

.PARAMETER KeyFile
Where the two API credential lines are. Default ~\.amo-api-key.

.PARAMETER WhatIf
Show what would happen and change nothing.

.EXAMPLE
.\Publish-Extension.ps1

.EXAMPLE
.\Publish-Extension.ps1 -Version 1.1.0 -WhatIf
#>
param(
    [string]$Version,
    [string]$KeyFile = (Join-Path $env:USERPROFILE '.amo-api-key'),
    [switch]$WhatIf
)

$ErrorActionPreference = 'Stop'
$repo = 'maxrenke/youtube-podcast-server'
$addonId = 'youtube-podcast@maxrenke.com'
$webExt = 'web-ext@10.7.0'
$source = Join-Path $PSScriptRoot 'firefox-extension'
$manifestPath = Join-Path $source 'manifest.json'
$updatesPath = Join-Path $PSScriptRoot 'firefox-extension-updates.json'
$keepVersions = 5   # how many releases the update file lists

Push-Location $PSScriptRoot
try {
    if (git status --porcelain -- firefox-extension firefox-extension-updates.json) {
        throw "[!] firefox-extension has uncommitted changes. Commit them first, so the release matches the repo."
    }

    # ---- version
    $manifestText = Get-Content -LiteralPath $manifestPath -Raw
    $current = ($manifestText | ConvertFrom-Json).version
    $released = @(gh release list --repo $repo --limit 200 --json tagName --jq '.[].tagName') -match '^extension-v'
    if (-not $Version) {
        if ($released -contains "extension-v$current") {
            $parts = $current.Split('.')
            $parts[-1] = [string]([int]$parts[-1] + 1)
            $Version = $parts -join '.'
        } else {
            $Version = $current
        }
    }
    if ($released -contains "extension-v$Version") { throw "[!] extension-v$Version has already been released." }
    $tag = "extension-v$Version"
    Write-Host "Releasing $addonId $Version (manifest has $current)"
    if ($WhatIf) {
        Write-Host "WhatIf: would set the manifest version to $Version, lint, sign as unlisted with $webExt,"
        Write-Host "        create GitHub release $tag with the signed .xpi, and add it to firefox-extension-updates.json"
        return
    }

    # ---- credentials (passed to web-ext through its environment variables, never on the command line)
    if (-not ($env:WEB_EXT_API_KEY -and $env:WEB_EXT_API_SECRET)) {
        if (-not (Test-Path -LiteralPath $KeyFile)) {
            throw "[!] No addons.mozilla.org credentials. Create them at https://addons.mozilla.org/en-US/developers/addon/api/key/ and save the JWT issuer and JWT secret as two lines in $KeyFile."
        }
        $lines = @(Get-Content -LiteralPath $KeyFile | Where-Object { $_.Trim() })
        if ($lines.Count -lt 2) { throw "[!] $KeyFile needs two lines: the JWT issuer, then the JWT secret." }
        $env:WEB_EXT_API_KEY = $lines[0].Trim()
        $env:WEB_EXT_API_SECRET = $lines[1].Trim()
    }

    # ---- version into the manifest (text replace keeps the file's formatting)
    if ($Version -ne $current) {
        $manifestText = $manifestText -replace ('"version":\s*"' + [regex]::Escape($current) + '"'), ('"version": "' + $Version + '"')
        [IO.File]::WriteAllText($manifestPath, $manifestText)
    }

    # ---- lint and sign
    Write-Host "==> Linting"
    npx --yes $webExt lint --source-dir $source --ignore-files README.md --self-hosted --warnings-as-errors
    if ($LASTEXITCODE) { throw "[!] web-ext lint found problems (see above)." }

    $artifacts = Join-Path ([IO.Path]::GetTempPath()) "youtube-podcast-extension-$Version"
    if (Test-Path -LiteralPath $artifacts) { Remove-Item -LiteralPath $artifacts -Recurse -Force }
    Write-Host "==> Signing with addons.mozilla.org (unlisted; usually a few minutes)"
    npx --yes $webExt sign --channel unlisted --source-dir $source --artifacts-dir $artifacts `
        --ignore-files README.md --approval-timeout 1800000
    if ($LASTEXITCODE) { throw "[!] Signing failed (see above). The manifest now says $Version; git checkout firefox-extension\manifest.json undoes that." }
    $xpi = Get-ChildItem -LiteralPath $artifacts -Filter *.xpi | Select-Object -First 1
    if (-not $xpi) { throw "[!] web-ext reported success but left no .xpi in $artifacts." }
    $assetName = "youtube-podcast-$Version.xpi"
    $assetPath = Join-Path $artifacts $assetName
    Move-Item -LiteralPath $xpi.FullName -Destination $assetPath

    # ---- publish: release first, then the update file that points at it
    git add -- firefox-extension/manifest.json
    if (git diff --cached --name-only) {
        git commit -q -m "extension: version $Version"
    }
    git push -q origin HEAD
    Write-Host "==> GitHub release $tag"
    gh release create $tag $assetPath --repo $repo --target (git rev-parse HEAD) `
        --title "Firefox extension $Version" `
        --notes "Signed by Mozilla for self-distribution. Open the .xpi in Firefox to install; installed copies update themselves."
    if ($LASTEXITCODE) { throw "[!] Creating the GitHub release failed." }

    $link = "https://github.com/$repo/releases/download/$tag/$assetName"
    $hash = (Get-FileHash -LiteralPath $assetPath -Algorithm SHA256).Hash.ToLower()
    $doc = Get-Content -LiteralPath $updatesPath -Raw | ConvertFrom-Json
    $entries = @([pscustomobject]@{ version = $Version; update_link = $link; update_hash = "sha256:$hash" }) +
               @($doc.addons.$addonId.updates | Where-Object { $_.version -ne $Version })
    $doc.addons.$addonId.updates = @($entries | Select-Object -First $keepVersions)
    [IO.File]::WriteAllText($updatesPath, ($doc | ConvertTo-Json -Depth 6) + "`n")
    git add -- firefox-extension-updates.json
    git commit -q -m "extension: publish $Version to the update channel"
    git push -q origin HEAD
    if ($LASTEXITCODE) { throw "[!] Pushing firefox-extension-updates.json failed; installed copies will not see $Version until it is pushed." }

    Write-Host ""
    Write-Host "[ok] $Version is signed and published."
    Write-Host "     Install (first time): open $link in Firefox and click Add."
    Write-Host "     Installed copies update within a day, or now via about:addons > gear > Check for Updates."
} finally {
    Pop-Location
}
