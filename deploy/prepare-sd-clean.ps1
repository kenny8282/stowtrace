# ============================================================================
# StowTrace SD/SSD preparation - CLEAN BOOT variant (no auto-install)
# ----------------------------------------------------------------------------
# Prepares a Pi that boots to a clean, reachable login - then you run the
# installer over SSH where every line is visible. Nothing can hang headless.
#
# After boot (~60-90s) it appears on your network. Then:
#   ssh stowtrace@<ip>        (key, or password 'stowtrace')
#   curl -sSL https://raw.githubusercontent.com/kenny8282/stowtrace/main/install.sh | sudo bash -s -- --no-bootstrap 2>&1 | tee ~/install.log
#
# Usage:
#   .\prepare-sd-clean.ps1 -DriveLetter D
#   .\prepare-sd-clean.ps1 -DriveLetter D -Hostname stowtrace
# ============================================================================

param(
    [string]$DriveLetter = "",
    [string]$Hostname = "stowtrace",
    [string[]]$PubKeyPaths = @()
)
$ErrorActionPreference = "Stop"
function Info($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "  [!]  $m" -ForegroundColor Yellow }
function Fail($m) { Write-Host "  [X]  $m" -ForegroundColor Red; exit 1 }

# ---- Gather SSH keys ----
Info "Pre-flight checks"
$keyFiles = @()
foreach ($p in $PubKeyPaths) {
    if (Test-Path $p) { $keyFiles += (Resolve-Path $p).Path } else { Warn "Key not found: $p" }
}
$defaultKey = "$env:USERPROFILE\.ssh\id_ed25519.pub"
if ((Test-Path $defaultKey) -and ($keyFiles -notcontains (Resolve-Path $defaultKey).Path)) {
    $keyFiles += (Resolve-Path $defaultKey).Path
}
$scriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { (Get-Location).Path }
$keysFolder = Join-Path $scriptDir "keys"
if (Test-Path $keysFolder) {
    Get-ChildItem -Path $keysFolder -Filter "*.pub" -File | ForEach-Object {
        if ($keyFiles -notcontains $_.FullName) { $keyFiles += $_.FullName }
    }
}
$keysSeen = @{}
$publicKeyLines = @()
foreach ($f in $keyFiles) {
    $content = (Get-Content $f -Raw).Trim()
    foreach ($line in ($content -split "`r?`n")) {
        $line = $line.Trim()
        if (-not $line) { continue }
        $parts = $line -split '\s+', 3
        if ($parts.Count -lt 2) { continue }
        $fp = $parts[1]
        if ($keysSeen.ContainsKey($fp)) { continue }
        $keysSeen[$fp] = $true
        $publicKeyLines += $line
        Ok "Key: $(Split-Path $f -Leaf)"
    }
}
$publicKey = $publicKeyLines -join "`n"
if ($publicKeyLines.Count -eq 0) {
    Warn "No SSH keys found - you can still log in with password 'stowtrace'."
} else {
    Ok "Total: $($publicKeyLines.Count) SSH key(s) trusted"
}

# ---- Find boot partition ----
Info "Locating boot partition"
if ($DriveLetter) {
    $candidateDrives = @($DriveLetter.TrimEnd(':') + ':')
} else {
    $allFsDrives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match '^[D-Z]:\' }
    $matchedDrives = @()
    foreach ($d in $allFsDrives) {
        if (Test-Path "$($d.Root)cmdline.txt") { $matchedDrives += $d.Root.TrimEnd('\') }
    }
    $candidateDrives = $matchedDrives
}
if (-not $candidateDrives -or $candidateDrives.Count -eq 0) { Fail "No Pi boot partition found (no cmdline.txt)" }
if ($candidateDrives.Count -gt 1) { Fail "Multiple boot partitions: $($candidateDrives -join ', ') - use -DriveLetter" }
$BootDrive = $candidateDrives[0]
if (-not $BootDrive.EndsWith('\')) { $BootDrive = "$BootDrive\" }
Ok "Boot partition: $BootDrive"
$cmdlinePath = Join-Path $BootDrive "cmdline.txt"
if (-not (Test-Path $cmdlinePath) -or -not (Test-Path (Join-Path $BootDrive "config.txt"))) {
    Fail "$BootDrive missing cmdline.txt or config.txt"
}

# ---- userconf ----
Info "Creating stowtrace user"
$hash = '$6$rounds=10000$XO5wHIuxiUyfKAld$y9aQrZ2dnL6Xv.W3T.MJgEN1Zw0bxRpAQ7nIuFQzHmoG5UNk4TZx7HMNZ5jHJl1mPmGuCQcfvUR8K9TkLsbDS.'
[System.IO.File]::WriteAllText((Join-Path $BootDrive "userconf.txt"), "stowtrace:$hash", [System.Text.Encoding]::ASCII)
Ok "userconf.txt written (stowtrace / stowtrace)"

# ---- SSH enable ----
New-Item -ItemType File -Force -Path (Join-Path $BootDrive "ssh") | Out-Null
Ok "SSH enabled"

# ---- Stage keys ----
if ($publicKeyLines.Count -gt 0) {
    New-Item -ItemType Directory -Force -Path (Join-Path $BootDrive "stowtrace") | Out-Null
    [System.IO.File]::WriteAllText((Join-Path $BootDrive "stowtrace\authorized_keys"), $publicKey + "`n", [System.Text.Encoding]::ASCII)
    Ok "SSH key(s) staged"
}

# ---- Hostname ----
[System.IO.File]::WriteAllText((Join-Path $BootDrive "hostname.txt"), $Hostname, [System.Text.Encoding]::ASCII)
Ok "Hostname: $Hostname"

# ---- firstrun.sh + service (base64 -> written as bytes, no PS parsing) ----
Info "Writing minimal firstrun (setup only - cannot hang)"
$frB64 = "IyEvYmluL2Jhc2gKIyBTdG93VHJhY2UgQ0xFQU4gZmlyc3QtYm9vdDogaG9zdG5hbWUgKyBTU0gga2V5IE9OTFkuIE5vIGluc3RhbGwuIE5vIHJlYm9vdC4Kc2V0ICtlCmV4ZWMgPiAvdmFyL2xvZy9zdG93dHJhY2UtZmlyc3RydW4ubG9nIDI+JjEKZWNobyAiWyQoZGF0ZSldIFN0b3dUcmFjZSBjbGVhbiBmaXJzdHJ1biBzdGFydGluZyIKaWYgWyAtZiAvYm9vdC9maXJtd2FyZS9ob3N0bmFtZS50eHQgXTsgdGhlbgogIE5FV19IT1NUTkFNRT0kKGNhdCAvYm9vdC9maXJtd2FyZS9ob3N0bmFtZS50eHQgfCB0ciAtZCAnWzpzcGFjZTpdJykKZWxpZiBbIC1mIC9ib290L2hvc3RuYW1lLnR4dCBdOyB0aGVuCiAgTkVXX0hPU1ROQU1FPSQoY2F0IC9ib290L2hvc3RuYW1lLnR4dCB8IHRyIC1kICdbOnNwYWNlOl0nKQplbHNlCiAgTkVXX0hPU1ROQU1FPSJzdG93dHJhY2UiCmZpCmVjaG8gIlNldHRpbmcgaG9zdG5hbWUgdG8gJyRORVdfSE9TVE5BTUUnIgpob3N0bmFtZWN0bCBzZXQtaG9zdG5hbWUgIiRORVdfSE9TVE5BTUUiIDI+L2Rldi9udWxsIHx8IHRydWUKc2VkIC1pICJzL14xMjdcLjBcLjFcLjEuKi8xMjcuMC4xLjFcdCRORVdfSE9TVE5BTUUvIiAvZXRjL2hvc3RzCmVjaG8gIkNvbmZpZ3VyaW5nIFNTSCBrZXkgZm9yIHN0b3d0cmFjZSB1c2VyIgpmb3IgaSBpbiB7MS4uMzB9OyBkbyBpZCBzdG93dHJhY2UgPi9kZXYvbnVsbCAyPiYxICYmIGJyZWFrOyBzbGVlcCAxOyBkb25lCmlmIGlkIHN0b3d0cmFjZSA+L2Rldi9udWxsIDI+JjE7IHRoZW4KICBpbnN0YWxsIC1kIC1tIDcwMCAtbyBzdG93dHJhY2UgLWcgc3Rvd3RyYWNlIC9ob21lL3N0b3d0cmFjZS8uc3NoCiAgZm9yIFNSQyBpbiAvYm9vdC9maXJtd2FyZS9zdG93dHJhY2UvYXV0aG9yaXplZF9rZXlzIC9ib290L3N0b3d0cmFjZS9hdXRob3JpemVkX2tleXM7IGRvCiAgICBpZiBbIC1mICIkU1JDIiBdOyB0aGVuCiAgICAgIGluc3RhbGwgLW0gNjAwIC1vIHN0b3d0cmFjZSAtZyBzdG93dHJhY2UgIiRTUkMiIC9ob21lL3N0b3d0cmFjZS8uc3NoL2F1dGhvcml6ZWRfa2V5cwogICAgICBlY2hvICJJbnN0YWxsZWQgYXV0aG9yaXplZF9rZXlzIGZyb20gJFNSQyIKICAgICAgYnJlYWsKICAgIGZpCiAgZG9uZQplbHNlCiAgZWNobyAiTk9URTogc3Rvd3RyYWNlIHVzZXIgbm90IHByZXNlbnQgeWV0IC0gcGFzc3dvcmQgbG9naW4gc3RpbGwgd29ya3MiCmZpCmVjaG8gIkRpc2FibGluZyBjbGVhbiBmaXJzdHJ1biBzZXJ2aWNlIgpzeXN0ZW1jdGwgZGlzYWJsZSBzdG93dHJhY2UtZmlyc3RydW4uc2VydmljZSAyPi9kZXYvbnVsbCB8fCB0cnVlCnJtIC1mIC9ldGMvc3lzdGVtZC9zeXN0ZW0vc3Rvd3RyYWNlLWZpcnN0cnVuLnNlcnZpY2UKZWNobyAiWyQoZGF0ZSldIENsZWFuIGZpcnN0cnVuIGNvbXBsZXRlLiBCb3ggdXAsIHJlYWNoYWJsZSwgcmVhZHkgZm9yIG1hbnVhbCBpbnN0YWxsLiIK"
$svB64 = "W1VuaXRdCkRlc2NyaXB0aW9uPVN0b3dUcmFjZSBjbGVhbiBmaXJzdC1ib290IHNldHVwIChob3N0bmFtZSArIFNTSCBrZXkgb25seSkKQWZ0ZXI9bXVsdGktdXNlci50YXJnZXQKW1NlcnZpY2VdClR5cGU9b25lc2hvdApFeGVjU3RhcnQ9L2Jpbi9iYXNoIC9ib290L2Zpcm13YXJlL2ZpcnN0cnVuLnNoClJlbWFpbkFmdGVyRXhpdD1ubwpUaW1lb3V0U3RhcnRTZWM9MTIwCltJbnN0YWxsXQpXYW50ZWRCeT1tdWx0aS11c2VyLnRhcmdldAo="
[System.IO.File]::WriteAllBytes((Join-Path $BootDrive "firstrun.sh"), [System.Convert]::FromBase64String($frB64))
[System.IO.File]::WriteAllBytes((Join-Path $BootDrive "stowtrace-firstrun.service"), [System.Convert]::FromBase64String($svB64))
Ok "firstrun.sh + service written"

# ---- Hook cmdline (enable service, NO reboot) ----
Info "Hooking cmdline.txt (clean - no reboot)"
$cmdline = (Get-Content $cmdlinePath -Raw).Trim()
if ($cmdline -match "stowtrace-clean") {
    Ok "Already hooked (skipping)"
} else {
    $hook = 'systemd.run=/bin/bash\ -c\ "cp\ /boot/firmware/stowtrace-firstrun.service\ /etc/systemd/system/\ 2>/dev/null\ ||\ cp\ /boot/stowtrace-firstrun.service\ /etc/systemd/system/;\ systemctl\ enable\ stowtrace-firstrun.service;\ touch\ /var/lib/stowtrace-clean" systemd.run_success_action=none'
    [System.IO.File]::WriteAllText($cmdlinePath, "$cmdline $hook", [System.Text.Encoding]::ASCII)
    Ok "cmdline.txt hooked (enables setup, keeps booting - no reboot)"
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  SD/SSD ready - CLEAN BOOT (no auto-install)" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  User: stowtrace / password 'stowtrace' (+ your SSH key)"
Write-Host "  Next: boot -> find on pfSense -> ssh stowtrace@<ip> -> run installer"
Write-Host "    curl -sSL https://raw.githubusercontent.com/kenny8282/stowtrace/main/install.sh | sudo bash -s -- --no-bootstrap 2>&1 | tee ~/install.log"
Write-Host ""
