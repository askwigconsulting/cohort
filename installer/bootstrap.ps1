<#
Cohort bootstrap (Windows / PowerShell) — clone-and-go installer.

Detects/creates a virtualenv, installs the package (only if needed), then runs
`cohort recompile`, passing --source explicitly plus any flags you supply:

    .\installer\bootstrap.ps1 --ide claude,cursor
    .\installer\bootstrap.ps1 --ide all --copy

The native-Windows counterpart to bootstrap.sh. Idempotent: re-running skips the
pip install when `cohort` is already importable in the venv. On Windows the
executor defaults to copy-mode (symlinks need Developer Mode/admin).

Override points (mainly for tests): COHORT_SOURCE, COHORT_VENV, COHORT_PYTHON,
COHORT_BIN.
#>
$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repo   = if ($env:COHORT_SOURCE) { $env:COHORT_SOURCE } else { (Resolve-Path (Join-Path $scriptDir '..')).Path }
$venv   = if ($env:COHORT_VENV)   { $env:COHORT_VENV }   else { Join-Path $repo '.venv' }
$python = if ($env:COHORT_PYTHON) { $env:COHORT_PYTHON } else { 'python' }

# venv layout differs by platform so this script is also exercisable via pwsh on
# POSIX CI; on Windows it's always Scripts\.
$binDir = if ($IsWindows -eq $false) { 'bin' } else { 'Scripts' }

# 1. Ensure a virtualenv exists.
if (-not (Test-Path -LiteralPath $venv)) {
    Write-Host "cohort: creating virtualenv at $venv"
    & $python -m venv $venv
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}
$venvPython = Join-Path $venv (Join-Path $binDir 'python')

# 2. Install the package only if it is not already importable (no reinstall churn).
& $venvPython -c 'import cohort' 2>$null
if ($LASTEXITCODE -eq 0) {
    Write-Host 'cohort: package already installed; skipping pip install'
} else {
    Write-Host "cohort: installing package into $venv"
    & $venvPython -m pip install -e $repo | Out-Null
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# 3. Compile + install, forwarding --source and the caller's flags. (recompile,
#    not install: a fresh machine has nothing staged yet, so install alone would
#    place the home but no agents.)
$cohortBin = if ($env:COHORT_BIN) { $env:COHORT_BIN } else { Join-Path $venv (Join-Path $binDir 'cohort') }
& $cohortBin recompile --source $repo @args
exit $LASTEXITCODE
