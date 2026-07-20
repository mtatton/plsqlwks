#!/bin/sh
set -eu

if [ "${1:-}" = "--all" ]; then
    profile="all"
    shift
else
    profile="${1:-core}"
    if [ "$#" -gt 0 ]; then
        shift
    fi
fi
exec python3 tools/dev.py test "$profile" "$@"
