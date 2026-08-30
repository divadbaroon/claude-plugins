#Requires -Version 5.1
# Engelbart installer for Windows: a self-contained binary, so the machine needs
# nothing first -- no Node, no npm, no Python. Everything after the download is
# the same engelbart CLI that npm installs; this script only fetches the right
# binary, checks its hash, and runs it. The Windows counterpart of install.sh.
#
#   irm https://berkeley.mathetic.com/engelbart/install.ps1 | iex
#
# `irm | iex` cannot forward arguments, so to pass a setup code use the
# scriptblock form, which binds the remaining arguments to this script:
#
#   & ([scriptblock]::Create((irm https://berkeley.mathetic.com/engelbart/install.ps1))) --code XXXX-XXXX-XXXX
param([Parameter(ValueFromRemainingArguments = $true)] [string[]] $ForwardArgs)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # the progress bar throttles the download

$repo = 'divadbaroon/claude-plugins'
$base = "https://github.com/$repo/releases/download/engelbart-latest"
# Bun compiles only x64 for Windows (there is no windows-arm64 target), and
# Windows-on-ARM runs x64 binaries under emulation, so one target covers both.
$target = 'engelbart-windows-x64.exe'

function Fail($message) { Write-Error "engelbart: $message"; exit 1 }

$dest = if ($env:ENGELBART_INSTALL_DIR) { $env:ENGELBART_INSTALL_DIR } `
        else { Join-Path $env:USERPROFILE '.local\bin' }
$tmp = Join-Path $env:TEMP ('engelbart-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
try {
  Write-Host "Downloading $target..."
  $binary = Join-Path $tmp $target
  Invoke-WebRequest -UseBasicParsing "$base/$target" -OutFile $binary
  Invoke-WebRequest -UseBasicParsing "$base/$target.sha256" -OutFile "$binary.sha256"

  # The .sha256 file is `<hex>  <filename>`; the first token is the hash.
  $expected = (((Get-Content "$binary.sha256" -Raw).Trim() -split '\s+')[0]).ToLower()
  $actual = (Get-FileHash -Algorithm SHA256 $binary).Hash.ToLower()
  if ($expected -ne $actual) { Fail 'downloaded binary failed its SHA-256 check' }

  New-Item -ItemType Directory -Force -Path $dest | Out-Null
  $installed = Join-Path $dest 'engelbart.exe'
  Move-Item -Force $binary $installed
  Write-Host "Installed $installed"

  $onPath = $env:PATH -split ';' | Where-Object {
    $_ -and ($_.TrimEnd('\') -ieq $dest.TrimEnd('\'))
  }
  if (-not $onPath) {
    Write-Host ''
    Write-Host "Note: $dest is not on your PATH. Add it for new terminals with:"
    Write-Host "    setx PATH `"$dest;`$env:PATH`""
  }

  # Hand off to the real installer; it explains everything from here.
  & $installed install @ForwardArgs
  exit $LASTEXITCODE
} finally {
  Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
}
