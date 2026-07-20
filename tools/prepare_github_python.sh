#!/usr/bin/env bash
set -euo pipefail

version="${1:-}"
case "$version" in
  3.10|3.14) ;;
  *)
    echo "CI preflight failed: expected Python 3.10 or 3.14, found ${version:-<empty>}" >&2
    exit 1
    ;;
esac

python_root="${PLSQLWKS_CI_PYTHON_ROOT:-/usr/local/bin}"
python_bin="$python_root/python$version"
if [ ! -x "$python_bin" ]; then
  echo "CI preflight failed: $python_bin is unavailable or not executable" >&2
  exit 1
fi

actual_version="$("$python_bin" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [ "$actual_version" != "$version" ]; then
  echo "CI preflight failed: $python_bin reports Python $actual_version, expected $version" >&2
  exit 1
fi

: "${RUNNER_TEMP:?CI preflight failed: RUNNER_TEMP is unavailable}"
: "${GITHUB_PATH:?CI preflight failed: GITHUB_PATH is unavailable}"
: "${GITHUB_RUN_ID:?CI preflight failed: GITHUB_RUN_ID is unavailable}"
: "${GITHUB_RUN_ATTEMPT:?CI preflight failed: GITHUB_RUN_ATTEMPT is unavailable}"
: "${GITHUB_JOB:?CI preflight failed: GITHUB_JOB is unavailable}"
test -d "$RUNNER_TEMP"
case "$GITHUB_RUN_ID:$GITHUB_RUN_ATTEMPT" in
  *[!0-9:]*|:*|*:) echo "CI preflight failed: invalid GitHub run identity" >&2; exit 1 ;;
esac

environment="$RUNNER_TEMP/plsqlwks-python-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}-${GITHUB_JOB}-${version}"
if [ -e "$environment" ]; then
  echo "CI preflight failed: Python environment already exists" >&2
  exit 1
fi

umask 077
"$python_bin" -m venv "$environment"
"$environment/bin/python" -c \
  'import sys; expected = sys.argv[1]; actual = f"{sys.version_info.major}.{sys.version_info.minor}"; assert actual == expected' \
  "$version"
printf '%s\n' "$environment/bin" >> "$GITHUB_PATH"
printf 'Prepared Python %s from %s\n' "$version" "$python_bin"
