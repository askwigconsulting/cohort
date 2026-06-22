#!/bin/sh
# Cohort bootstrap — clone-and-go installer.
#
# Detects/creates a virtualenv, installs the package (only if needed), then runs
# `cohort install`, passing --source explicitly plus any flags you supply:
#
#     ./installer/bootstrap.sh --ide claude,cursor
#     ./installer/bootstrap.sh --ide all --copy
#
# POSIX-sh; works on macOS / Linux / WSL. Idempotent: re-running skips the pip
# install when `cohort` is already importable in the venv.
#
# Override points (mainly for tests): COHORT_SOURCE, COHORT_VENV, COHORT_PYTHON,
# COHORT_BIN.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
repo=${COHORT_SOURCE:-$(CDPATH='' cd -- "$script_dir/.." && pwd)}
venv=${COHORT_VENV:-"$repo/.venv"}
python=${COHORT_PYTHON:-python3}

# 1. Ensure a virtualenv exists.
if [ ! -d "$venv" ]; then
    echo "cohort: creating virtualenv at $venv"
    "$python" -m venv "$venv"
fi
venv_python="$venv/bin/python"

# 2. Install the package only if it is not already importable (no reinstall churn).
if "$venv_python" -c 'import cohort' >/dev/null 2>&1; then
    echo "cohort: package already installed; skipping pip install"
else
    echo "cohort: installing package into $venv"
    "$venv_python" -m pip install -e "$repo" >/dev/null
fi

# 3. Run the installer, forwarding --source and the caller's flags.
cohort_bin=${COHORT_BIN:-"$venv/bin/cohort"}
exec "$cohort_bin" install --source "$repo" "$@"
