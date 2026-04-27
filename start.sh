#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_PATH=${1:-"$SCRIPT_DIR/references/default-config.toml"}

cd "$SCRIPT_DIR"

echo "Starting Hermes Paper Monitor"
echo "Config: $CONFIG_PATH"
echo "URL: http://127.0.0.1:8765/"

exec python3 "$SCRIPT_DIR/scripts/paper_pipeline.py" \
  --config "$CONFIG_PATH" \
  serve
