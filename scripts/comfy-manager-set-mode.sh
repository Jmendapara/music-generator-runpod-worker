#!/usr/bin/env bash
# comfy-manager-set-mode: set the ComfyUI-Manager network mode.
# Usage: comfy-manager-set-mode <mode>
#   <mode> is one of: online, offline, local
set -euo pipefail

MODE="${1:-offline}"
CONFIG_DIR="/comfyui/custom_nodes/ComfyUI-Manager"
CONFIG_FILE="${CONFIG_DIR}/config.ini"

if [[ ! -d "$CONFIG_DIR" ]]; then
  echo "ComfyUI-Manager not found at ${CONFIG_DIR}" >&2
  exit 1
fi

# Create or update config.ini with the desired network mode
cat > "$CONFIG_FILE" <<EOF
[default]
network_mode = ${MODE}
EOF

echo "ComfyUI-Manager network_mode set to: ${MODE}"
