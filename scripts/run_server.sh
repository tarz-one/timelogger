#!/bin/zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_DIR"
exec "$PROJECT_DIR/.venv/bin/uvicorn" app:app --host 127.0.0.1 --port 8000
