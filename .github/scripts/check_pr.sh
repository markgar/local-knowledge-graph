#!/usr/bin/env bash
set -eu
if [[ ! -x .venv/bin/python || ! -f .github/scripts/check_pr.py ]]; then
  printf '%s\n' 'Run from the repository root with a prepared .venv.' >&2
  exit 2
fi
# Exec preserves the public launcher's PID so cancellation reaches its supervisor.
exec .venv/bin/python .github/scripts/check_pr.py --launch "$@"
