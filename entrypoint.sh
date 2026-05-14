#!/usr/bin/env bash
# Entrypoint for the multivon-ai/eval-action container action.
#
# GitHub passes the inputs declared in action.yml as positional args
# in the order listed there. We forward them to the Python runner so
# the runner stays trivially testable outside the container.

set -euo pipefail
exec python -m src.runner "$@"
