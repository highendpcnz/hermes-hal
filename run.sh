#!/usr/bin/env bash
# Launch the Hermes Hal web frontend for Hermes Agent.
# Runs inside the Hermes venv — no separate environment needed.
# Plain bash so it works on Linux as well as macOS (zsh runs it fine too).
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
PORT="${HAL_PORT:-8000}"
HOST="${HAL_HOST:-127.0.0.1}"
export HAL_UI="${HAL_UI:-lite}"

# The venv that RUNS HAL needs fastapi/uvicorn/piper/faster-whisper. That is
# normally the Hermes venv, but a repo-local .venv takes precedence when one
# exists: some Hermes installs ship without the STT/TTS stack, and preferring
# Hermes would select an environment that cannot even import main.py.
if [[ -n "${HAL_HERMES_VENV:-}" ]]; then
  HERMES_VENV="$HAL_HERMES_VENV"
elif [[ -x "$APP_DIR/.venv/bin/uvicorn" ]]; then
  HERMES_VENV="$APP_DIR/.venv"
elif [[ -x "$HOME/.hermes/hermes-agent/venv/bin/uvicorn" ]]; then
  HERMES_VENV="$HOME/.hermes/hermes-agent/venv"
elif [[ -x "$HOME/hermes-agent/.venv/bin/uvicorn" ]]; then
  HERMES_VENV="$HOME/hermes-agent/.venv"
else
  echo "No Python environment with uvicorn was found." >&2
  echo "Install Hermes Agent, create a repo-local .venv from requirements.txt," >&2
  echo "or set HAL_HERMES_VENV to an environment path." >&2
  exit 1
fi

# The agent binaries are spawned as subprocesses, so they are independent of the
# venv running HAL — resolve them against a real Hermes install rather than
# assuming they sit beside uvicorn. An isolated .venv has neither.
_find_hermes_bin() {
  local name="$1" candidate
  for candidate in "$HERMES_VENV/bin/$name" \
                   "$HOME/.hermes/hermes-agent/venv/bin/$name" \
                   "$HOME/hermes-agent/.venv/bin/$name"; do
    [[ -x "$candidate" ]] && { printf '%s\n' "$candidate"; return; }
  done
  command -v "$name" 2>/dev/null || printf '%s\n' "$HERMES_VENV/bin/$name"
}

export HAL_HERMES_BIN="${HAL_HERMES_BIN:-$(_find_hermes_bin hermes)}"
export HAL_HERMES_ACP_BIN="${HAL_HERMES_ACP_BIN:-$(_find_hermes_bin hermes-acp)}"

# Fall back to the repo-local voice model if the shared Hermes voice install
# is absent (e.g. a machine where ~/.hermes/voices was never populated).
if [[ -z "${HAL_VOICE:-}" ]]; then
  DEFAULT_VOICE="$HOME/.hermes/voices/hal9000/hal9000.onnx"
  if [[ ! -f "$DEFAULT_VOICE" && -f "$APP_DIR/models/hal.onnx" ]]; then
    export HAL_VOICE="$APP_DIR/models/hal.onnx"
  fi
fi

# Fail fast if the port is taken — before loading two ML models and an agent.
if curl -sf -m 2 "http://$HOST:$PORT/api/health" >/dev/null 2>&1; then
  echo "HAL is already running at http://$HOST:$PORT" >&2
  exit 0
fi
if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "Port $PORT is in use by another process (not HAL)." >&2
  echo "Pick a different port: HAL_PORT=8001 $0" >&2
  exit 1
fi

exec "$HERMES_VENV/bin/uvicorn" main:app \
  --app-dir "$APP_DIR" \
  --host "$HOST" \
  --port "$PORT"
