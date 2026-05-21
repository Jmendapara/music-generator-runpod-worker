#!/usr/bin/env bash

# Activate the Python virtual environment
export VIRTUAL_ENV="/opt/venv"
export PATH="/opt/venv/bin:${PATH}"

# Use libtcmalloc for better memory management
TCMALLOC="$(ldconfig -p | grep -Po "libtcmalloc.so.\d" | head -n 1)"
export LD_PRELOAD="${TCMALLOC}"

# Ensure ComfyUI-Manager runs in offline network mode inside the container
comfy-manager-set-mode offline || echo "worker-comfyui - Could not set ComfyUI-Manager network_mode" >&2

# ---------- Diagnostics: model detection ----------
echo "worker-comfyui: Detecting models..."
for d in diffusion_models text_encoders vae; do
    if [ -d "/comfyui/models/${d}" ]; then
        echo "  /comfyui/models/${d}:"
        ls -la "/comfyui/models/${d}/" 2>/dev/null | sed 's/^/    /'
    else
        echo "  /comfyui/models/${d}: MISSING"
    fi
done

echo "  /runpod-volume exists: $([ -d /runpod-volume ] && echo YES || echo NO)"

# ---------- Pre-launch diagnostics ----------
echo "worker-comfyui: Python: $(which python) ($(python --version 2>&1))"
echo "worker-comfyui: System info before launch:"
echo "  GPU(s):"
nvidia-smi --query-gpu=gpu_name,memory.total,driver_version,compute_cap --format=csv,noheader 2>/dev/null \
    || echo "  (nvidia-smi not available)"
echo "  CUDA runtime version:"
python -c "import torch; print(f'  PyTorch {torch.__version__}, CUDA {torch.version.cuda}')" 2>/dev/null \
    || echo "  (torch not importable)"
echo "  System RAM:"
free -h 2>/dev/null | head -2 || echo "  (free not available)"
echo ""

echo "worker-comfyui: Starting ComfyUI"

EXTRA_PATHS="--extra-model-paths-config /comfyui/extra_model_paths.yaml"
COMFY_LOG="/var/log/comfyui.log"
COMFY_CMD="python -u /comfyui/main.py --disable-auto-launch --disable-metadata ${EXTRA_PATHS} --verbose INFO --log-stdout"

COMFY_RESTART_DELAY=5
COMFY_MAX_RAPID_RESTARTS=5
COMFY_RAPID_RESTART_WINDOW=60

comfyui_restart_loop() {
    set -o pipefail
    local rapid_count=0
    local window_start
    window_start=$(date +%s)

    while true; do
        echo "worker-comfyui: Launching ComfyUI process..."
        ${COMFY_CMD} 2>&1 | tee "${COMFY_LOG}"
        local exit_code=$?

        echo "worker-comfyui: ComfyUI exited with code ${exit_code}"

        local now
        now=$(date +%s)
        if (( now - window_start < COMFY_RAPID_RESTART_WINDOW )); then
            rapid_count=$((rapid_count + 1))
        else
            rapid_count=1
            window_start=$now
        fi

        if (( rapid_count >= COMFY_MAX_RAPID_RESTARTS )); then
            echo "worker-comfyui: FATAL — ComfyUI crashed ${rapid_count} times within ${COMFY_RAPID_RESTART_WINDOW}s, not restarting."
            return 1
        fi

        echo "worker-comfyui: Restarting ComfyUI in ${COMFY_RESTART_DELAY}s (crash ${rapid_count}/${COMFY_MAX_RAPID_RESTARTS} in window)..."
        sleep "${COMFY_RESTART_DELAY}"
    done
}

comfyui_restart_loop &
COMFY_LOOP_PID=$!
echo "worker-comfyui: ComfyUI restart loop PID=${COMFY_LOOP_PID}, log=${COMFY_LOG}"

# ---------- Startup readiness gate ----------
COMFY_STARTUP_CHECK_INTERVAL=1
COMFY_STARTUP_CHECK_MAX_TRIES=120

echo "worker-comfyui: Waiting for ComfyUI API at http://127.0.0.1:8188/ (max ${COMFY_STARTUP_CHECK_MAX_TRIES}s)..."
comfy_ready=0
for i in $(seq 1 "${COMFY_STARTUP_CHECK_MAX_TRIES}"); do
    if ! kill -0 "${COMFY_LOOP_PID}" 2>/dev/null; then
        echo "worker-comfyui: FATAL — ComfyUI restart loop exited before API became reachable."
        echo "worker-comfyui: Exiting container so RunPod retires this worker."
        exit 1
    fi

    if curl -sf -o /dev/null --max-time 2 http://127.0.0.1:8188/ 2>/dev/null; then
        comfy_ready=1
        echo "worker-comfyui: ComfyUI API is reachable (took ${i}s)."
        break
    fi
    sleep "${COMFY_STARTUP_CHECK_INTERVAL}"
done

if [ "${comfy_ready}" -eq 0 ]; then
    echo "worker-comfyui: FATAL — ComfyUI API not reachable after ${COMFY_STARTUP_CHECK_MAX_TRIES}s."
    echo "worker-comfyui: Exiting container so RunPod retires this worker."
    kill "${COMFY_LOOP_PID}" 2>/dev/null || true
    exit 1
fi

# ---------- Background monitor: kill handler if ComfyUI loop dies ----------
(
    while kill -0 "${COMFY_LOOP_PID}" 2>/dev/null; do
        sleep 5
    done
    echo "worker-comfyui: ComfyUI restart loop exited — terminating handler process."
    kill $$ 2>/dev/null || true
) &

echo "worker-comfyui: Starting RunPod Handler"
python -u /handler.py
