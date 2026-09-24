<#
.SYNOPSIS
  Put secrets in both places Tally reads them: the repo-root .env (local dev)
  and the linked Vercel project's Production environment (deploys).

.DESCRIPTION
  Prompts for each value with the input hidden, so a secret never shows on
  screen, in a command line, or in PowerShell history. In .env an existing
  NAME= line is replaced, otherwise one is appended. In Vercel the variable is
  stored as Sensitive (unreadable once saved) and --force overwrites an
  existing one, so re-running the script is also how you rotate a secret.
  Vercel only uses a new value after the next deploy.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\set-env.ps1 OURA_CLIENT_ID OURA_CLIENT_SECRET
#>
param(
  [Parameter(Mandatory = $true, ValueFromRemainingArguments = $true)]
  [string[]] $Names
)

$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$envFile = Join-Path $root ".env"
# Windows PowerShell pipes to native programs as ASCII by default; send UTF-8.
$OutputEncoding = New-Object System.Text.UTF8Encoding $false

if (-not (Get-Command vercel -ErrorAction SilentlyContinue)) {
  throw "The Vercel CLI is not on PATH. Install it with: npm i -g vercel"
}
foreach ($name in $Names) {
  if ($name -cnotmatch '^[A-Z_][A-Z0-9_]*$') {
    throw "'$name' is not a valid variable name (A-Z, 0-9 and _ only). Nothing changed."
  }
}

# Settle the Vercel login BEFORE any secret is piped. If the CLI has to start
# its login flow in the middle of `env add`, that flow reads stdin and its
# prompt echoes the piped value to the screen (2026-09-21: it printed a secret).
vercel whoami --cwd $root
if ($LASTEXITCODE -ne 0) {
  throw "Vercel is not logged in. Run 'vercel login', then this script again. Nothing changed."
}

$failed = @()
foreach ($name in $Names) {
  $secure = Read-Host -AsSecureString "Value for $name"
  $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { $value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr); $secure.Dispose() }
  if ([string]::IsNullOrEmpty($value)) {
    Write-Host "No value entered for $name - skipped." -ForegroundColor Yellow
    continue
  }

  # Quote anything a .env parser could misread: Node's dotenv cuts an unquoted
  # value at '#'. Single quotes are literal in both Node and python-dotenv.
  $entry = "$name=$value"
  if ($value -notmatch '^[A-Za-z0-9_\-.:/+=@]+$') {
    if ($value.Contains("'")) {
      Write-Host "The value for $name contains a single quote; add it to .env and Vercel by hand." -ForegroundColor Red
      $failed += $name; Remove-Variable value; continue
    }
    $entry = "$name='$value'"
  }

  # .env: replace the NAME= line if there is one, else append.
  $lines = @()
  if (Test-Path $envFile) { $lines = @([IO.File]::ReadAllLines($envFile)) }
  $pattern = '^\s*' + [regex]::Escape($name) + '\s*='
  $found = $false
  $lines = @(foreach ($line in $lines) {
    if ($line -match $pattern) { $found = $true; $entry } else { $line }
  })
  if (-not $found) { $lines += $entry }
  [IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))
  Write-Host "$name -> .env ($(if ($found) { 'replaced' } else { 'added' }))"

  # Vercel: the value goes in on stdin, never on the command line.
  $value | vercel env add $name production --sensitive --force --yes --cwd $root
  if ($LASTEXITCODE -ne 0) { $failed += $name } else { Write-Host "$name -> Vercel production" }
  Remove-Variable value
}

if ($failed.Count -gt 0) {
  Write-Host "Not set: $($failed -join ', '). See the messages above." -ForegroundColor Red
  exit 1
}
