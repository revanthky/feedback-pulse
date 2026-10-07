#!/usr/bin/env bash
# Start Feedback Pulse in the workspace. Publish it first:
#
#   d3x app create --name feedback-pulse --display-name "Feedback Pulse" \
#                  --description "LLM-powered customer feedback analysis" --icon icon.svg
#
# That writes .dkubex-app.env next to this script. Nothing supervises the app, so this is what
# actually makes the tile serve something — leave it running (or run it under tmux/nohup).
set -euo pipefail
cd "$(dirname "$0")"

# PORT and DKUBEX_BASE_PATH come from the workspace, not from this file. `set -a` exports every
# variable the file defines so the server below inherits them.
set -a
. ./.dkubex-app.env
set +a

# Use the dedicated virtualenv created for this app.
source .venv/bin/activate

# --server.baseUrlPath is what makes Streamlit emit correct asset/websocket URLs behind the
# workspace prefix; --server.address 0.0.0.0 is required because nginx reaches the app over the
# pod's loopback interface.
exec streamlit run app.py \
  --server.address 0.0.0.0 \
  --server.port "$PORT" \
  --server.baseUrlPath "$DKUBEX_BASE_PATH" \
  --server.headless true
