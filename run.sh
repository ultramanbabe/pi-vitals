#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/server"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8765}"
export GPU_MODEL="${GPU_MODEL:-ASUS PRIME Radeon RX 9070 XT}"
export RAM_MODEL="${RAM_MODEL:-Klevv Bolt V · 6000 CL28}"
exec ../.venv/bin/uvicorn main:app --host "$HOST" --port "$PORT" --log-level warning
