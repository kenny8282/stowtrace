# ============================================================================
# StowTrace SD/SSD preparation — CLEAN BOOT variant (no auto-install)
# ----------------------------------------------------------------------------
# This is the BULLETPROOF path. Unlike prepare-sd.ps1 (which auto-installs on
# first boot and can hang headless), this script prepares a Pi that boots to a
# clean, reachable login — then you run the installer over SSH where every line
# is visible and any failure shows itself instead of freezing the console.
#
# What it does:
#   1. Finds the freshly flashed boot partition (FAT32 with cmdline.txt)
#   2. Writes userconf to create the 'stowtrace' user (password 'stowtrace')
#   3. Enables SSH and stages your public key(s)
#   4. Drops a MINIMAL firstrun.sh that ONLY sets hostname + installs the SSH
#      key + leaves password auth ON as a fallback. It does NOT install
#      StowTrace and does NOT reboot. Nothing that can hang.
#   5. Hooks cmdline.txt to run that minimal firstrun once.
#
# After it boots (~60-90s) it appears on your network. Then:
#   ssh stowtrace@<ip>        (key, or password 'stowtrace')
#   curl -sSL https://raw.githubusercontent.com/kenny8282/stowtrace/main/install.sh | sudo bash -s -- --no-bootstrap 2>&1 | tee ~/install.log
#
# Usage:
#   .\prepare-sd-clean.ps1                 # auto-detect drive, use ~/.ssh/id_ed25519.pub + ./keys/*.pub
#   .\prepare-sd-clean.ps1 -DriveLetter E
#   .\prepare-sd-clean.ps1 -Hostname stowtrace
# ============================================================================

param(
    [string]$DriveLetter = "",
    [string]$Hostname = "stowtrace",
    [string[]]$PubKeyPaths = @()
)

$ErrorActionPreference = "Stop"

function Info($msg)    { Write-Host "==> $msg" -ForegroundColor Cyan }
function Ok($msg)      { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Warn($msg)    { Write-Host "  [!]  $msg" -ForegroundColor Yellow }
function Fail($msg)    { Write-Host "  [X]  $msg" -ForegroundColor Red; exit 1 }

# ---- Pre-flight: gather SSH keys ------------------------------------------
Info "Pre-flight checks"
$keyFiles = @()
foreach ($p in $PubKeyPaths) {
    if (Test-Path $p) { $keyFiles += (Resolve-Path $p).Path }
    else { Warn "Key file not found, skipping: $p" }
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
if ($keyFiles.Count -eq 0) {
    Warn "No SSH public keys found. That's OK for the clean-boot path —"
    Warn "you can still log in with password 'stowtrace' and add a key later."
    $publicKey = ""
    $publicKeyLines = @()
} else {
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
            $name = Split-Path $f -Leaf
            $shortFp = if ($fp.Length -gt 16) { $fp.Substring($fp.Length - 16) } else { $fp }
            Ok "Key: $name (...$shortFp)"
        }
    }
    $publicKey = $publicKeyLines -join "`n"
    Ok "Total: $($publicKeyLines.Count) SSH key(s) will be trusted on the Pi"
}

# ---- Find the boot partition ----------------------------------------------
Info "Locating boot partition"
if ($DriveLetter) {
    $candidateDrives = @($DriveLetter.TrimEnd(':') + ':')
} else {
    $allFsDrives = Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -match '^[D-Z]:\\' }
    Write-Host "  Scanning drives: $(($allFsDrives | ForEach-Object { $_.Root }) -join ', ')"
    $matchedDrives = @()
    foreach ($d in $allFsDrives) {
        if (Test-Path "$($d.Root)cmdline.txt") { $matchedDrives += $d.Root.TrimEnd('\') }
    }
    $candidateDrives = $matchedDrives
}
if (-not $candidateDrives -or $candidateDrives.Count -eq 0) {
    Warn "No drive contains cmdline.txt - boot partition not detected."
    Get-PSDrive -PSProvider FileSystem | Where-Object Root -match '^[A-Z]:\\' | ForEach-Object {
        $hasCmd = if (Test-Path "$($_.Root)cmdline.txt") { "YES" } else { "no" }
        Write-Host ("    {0,-6}  cmdline.txt={1}" -f $_.Root, $hasCmd)
    }
    Fail "Aborting - cannot find a Pi boot partition"
}
if ($candidateDrives.Count -gt 1) {
    Warn "Multiple boot partitions found: $($candidateDrives -join ', ')"
    Fail "Specify with: .\prepare-sd-clean.ps1 -DriveLetter E"
}
$BootDrive = $candidateDrives[0]
if (-not $BootDrive.EndsWith('\')) { $BootDrive = "$BootDrive\" }
Ok "Boot partition: $BootDrive"

$cmdlinePath = Join-Path $BootDrive "cmdline.txt"
$configPath  = Join-Path $BootDrive "config.txt"
if (-not (Test-Path $cmdlinePath) -or -not (Test-Path $configPath)) {
    Fail "$BootDrive doesn't look like a Pi boot partition (missing cmdline.txt or config.txt)"
}

# ---- Create the stowtrace user (userconf.txt) -----------------------------
Info "Creating stowtrace user"
$stowtracePwdHash = '$6$rounds=10000$XO5wHIuxiUyfKAld$y9aQrZ2dnL6Xv.W3T.MJgEN1Zw0bxRpAQ7nIuFQzHmoG5UNk4TZx7HMNZ5jHJl1mPmGuCQcfvUR8K9TkLsbDS.'
@"
stowtrace:$stowtracePwdHash
"@ | Out-File -Encoding ASCII -NoNewline -FilePath (Join-Path $BootDrive "userconf.txt")
Ok "userconf.txt written (user 'stowtrace', password 'stowtrace')"

# ---- Enable SSH -----------------------------------------------------------
Info "Enabling SSH"
New-Item -ItemType File -Force -Path (Join-Path $BootDrive "ssh") | Out-Null
Ok "SSH enabled"

# ---- Stage SSH key(s) -----------------------------------------------------
if ($publicKeyLines.Count -gt 0) {
    Info "Staging $($publicKeyLines.Count) SSH public key(s)"
    New-Item -ItemType Directory -Force -Path (Join-Path $BootDrive "stowtrace") | Out-Null
    $authKeyPath = Join-Path $BootDrive "stowtrace\authorized_keys"
    $lf = $publicKey + "`n"
    [System.IO.File]::WriteAllText($authKeyPath, $lf, [System.Text.Encoding]::ASCII)
    Ok "Public key(s) staged"
}

# ---- Hostname -------------------------------------------------------------
Info "Setting hostname"
$Hostname | Out-File -Encoding ASCII -NoNewline -FilePath (Join-Path $BootDrive "hostname.txt")
Ok "Hostname set to '$Hostname'"

# ---- Minimal firstrun.sh (NO install, NO reboot, NO network wait) ---------
Info "Writing minimal firstrun.sh (setup only - does not install or hang)"
$firstRunScript = @'
#!/bin/bash
# StowTrace CLEAN first-boot: hostname + SSH key ONLY. No install. No reboot.
# Deliberately cannot hang: no network wait, no apt, no comitup.
set +e
exec > /var/log/stowtrace-firstrun.log 2>&1
echo "[$(date)] StowTrace clean firstrun starting"

# Hostname
if [ -f /boot/firmware/hostname.txt ]; then
  NEW_HOSTNAME=$(cat /boot/firmware/hostname.txt | tr -d '[:space:]')
elif [ -f /boot/hostname.txt ]; then
  NEW_HOSTNAME=$(cat /boot/hostname.txt | tr -d '[:space:]')
else
  NEW_HOSTNAME="stowtrace"
fi
echo "Setting hostname to '$NEW_HOSTNAME'"
hostnamectl set-hostname "$NEW_HOSTNAME" 2>/dev/null || true
sed -i "s/^127\.0\.1\.1.*/127.0.1.1\t$NEW_HOSTNAME/" /etc/hosts

# SSH key for stowtrace user (wait for userconf to create the account)
echo "Configuring SSH key for stowtrace user"
for i in {1..30}; do id stowtrace >/dev/null 2>&1 && break; sleep 1; done
if id stowtrace >/dev/null 2>&1; then
  install -d -m 700 -o stowtrace -g stowtrace /home/stowtrace/.ssh
  for SRC in /boot/firmware/stowtrace/authorized_keys /boot/stowtrace/authorized_keys; do
    if [ -f "$SRC" ]; then
      install -m 600 -o stowtrace -g stowtrace "$SRC" /home/stowtrace/.ssh/authorized_keys
      echo "Installed authorized_keys from $SRC"
      break
    fi
  done
else
  echo "NOTE: stowtrace user not present yet - password login still works"
fi

# NOTE: password auth is deliberately LEFT ON as a fallback.
# Harden to key-only AFTER you confirm key login works, via install.sh.

# Disable this firstrun so it doesn't run again
echo "Disabling clean firstrun service"
systemctl disable stowtrace-firstrun.service 2>/dev/null || true
rm -f /etc/systemd/system/stowtrace-firstrun.service
echo "[$(date)] Clean firstrun complete. Box is up, reachable, ready for manual install."
'@
$firstRunScript -replace "`r`n", "`n" | Out-File -Encoding ASCII -NoNewline -FilePath (Join-Path $BootDrive "firstrun.sh")
Ok "firstrun.sh written (setup-only)"

# ---- Service unit that runs the minimal firstrun --------------------------
$serviceUnit = @'
[Unit]
Description=StowTrace clean first-boot setup (hostname + SSH key only)
After=multi-user.target
[Service]
Type=oneshot
ExecStart=/bin/bash /boot/firmware/firstrun.sh
RemainAfterExit=no
TimeoutStartSec=120
[Install]
WantedBy=multi-user.target
'@
$serviceUnit -replace "`r`n", "`n" | Out-File -Encoding ASCII -NoNewline -FilePath (Join-Path $BootDrive "stowtrace-firstrun.service")
Ok "Service unit staged"

# ---- Hook cmdline.txt (install+enable the service, then continue booting) --
# CRITICAL DIFFERENCE from prepare-sd.ps1: this does NOT reboot. It enables the
# oneshot and lets boot proceed normally. Nothing blocks the console.
Info "Hooking cmdline.txt (clean - no reboot)"
$cmdline = (Get-Content $cmdlinePath -Raw).Trim()
$MARKER = "stowtrace-clean"
if ($cmdline -match $MARKER) {
    Ok "cmdline.txt already hooked (skipping)"
} else {
    $bootstrap = 'systemd.run=/bin/bash\ -c\ "cp\ /boot/firmware/stowtrace-firstrun.service\ /etc/systemd/system/\ 2>/dev/null\ ||\ cp\ /boot/stowtrace-firstrun.service\ /etc/systemd/system/;\ systemctl\ enable\ stowtrace-firstrun.service;\ touch\ /var/lib/' + $MARKER + '" systemd.run_success_action=none'
    $newCmdline = "$cmdline $bootstrap"
    [System.IO.File]::WriteAllText($cmdlinePath, $newCmdline, [System.Text.Encoding]::ASCII)
    Ok "cmdline.txt hooked (enables setup service, keeps booting - no reboot)"
}

Write-Host ""
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  SD/SSD ready - CLEAN BOOT (no auto-install)" -ForegroundColor Green
Write-Host "================================================================" -ForegroundColor Green
Write-Host "  Hostname:    $Hostname"
Write-Host "  User:        stowtrace  /  password 'stowtrace'  (+ your SSH key)"
Write-Host "  Password auth: LEFT ON as fallback (harden later via install)"
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor Cyan
Write-Host "    1. Eject, put SSD in the Pi, ethernet + power on"
Write-Host "    2. Wait ~60-90s. It boots clean and appears on your network"
Write-Host "       (pfSense - Status - DHCP Leases - look for '$Hostname')"
Write-Host "    3. SSH in:   ssh stowtrace@<ip>"
Write-Host "    4. Install, watching every line:"
Write-Host "       curl -sSL https://raw.githubusercontent.com/kenny8282/stowtrace/main/install.sh | sudo bash -s -- --no-bootstrap 2>&1 | tee ~/install.log"
Write-Host ""
