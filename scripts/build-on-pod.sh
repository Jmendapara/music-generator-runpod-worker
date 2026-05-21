#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Build Docker image on a Hetzner/RunPod server and push to Docker Hub.
#
# Usage:
#   export DOCKERHUB_USERNAME="your-dockerhub-user"
#   export DOCKERHUB_TOKEN="your-dockerhub-access-token"
#   export IMAGE_TAG="your-user/music-generator-runpod-worker:latest"
#   curl -fsSL https://raw.githubusercontent.com/Jmendapara/music-generator-runpod-worker/main/scripts/build-on-pod.sh | bash
# =============================================================================

MODEL_TYPE="${MODEL_TYPE:-ace-step-1.5-xl-sft}"
COMFYUI_VERSION="${COMFYUI_VERSION:-latest}"
REPO_URL="${REPO_URL:-https://github.com/Jmendapara/music-generator-runpod-worker.git}"
BRANCH="${BRANCH:-main}"

: "${DOCKERHUB_USERNAME:?Set DOCKERHUB_USERNAME}"
: "${DOCKERHUB_TOKEN:?Set DOCKERHUB_TOKEN}"
: "${IMAGE_TAG:?Set IMAGE_TAG}"

# Install Docker if needed
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi

# Start Docker if needed
if ! docker info &>/dev/null 2>&1; then
    systemctl start docker 2>/dev/null || { dockerd &>/dev/null & sleep 5; }
fi

# Login
echo "${DOCKERHUB_TOKEN}" | docker login --username "${DOCKERHUB_USERNAME}" --password-stdin

# Free disk
docker system prune -af --volumes 2>/dev/null || true
docker builder prune -af 2>/dev/null || true

# Clone
rm -rf /tmp/build-workspace
git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" /tmp/build-workspace
cd /tmp/build-workspace

# Build
docker buildx build \
    --platform linux/amd64 \
    --target final \
    --no-cache \
    --build-arg "MODEL_TYPE=${MODEL_TYPE}" \
    --build-arg "COMFYUI_VERSION=${COMFYUI_VERSION}" \
    -t "${IMAGE_TAG}" .

# Push
docker push "${IMAGE_TAG}"

echo ""
echo "Done! Image pushed: ${IMAGE_TAG}"
