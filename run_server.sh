#!/bin/bash
# Start the common (v2) server on ws://localhost:8765.
set -e
cd "$(dirname "$0")"

if [ ! -d .venv ]; then
    echo "No .venv found. Run 'uv sync --all-extras' first." >&2
    exit 1
fi

source .venv/bin/activate
exec python -m src.mud.net.v2_server
