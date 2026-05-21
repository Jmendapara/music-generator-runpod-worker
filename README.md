# music-generator-runpod-worker

> [ACE-Step 1.5](https://github.com/ace-step/ACE-Step) text-to-music running on [ComfyUI](https://github.com/comfyanonymous/ComfyUI) as a serverless API on [RunPod](https://www.runpod.io/)

Generate full-band music — instrumentals or vocals with lyrics — from a text prompt. The default build bakes in the **XL SFT** variant (`acestep_v1.5_xl_sft_bf16.safetensors`), the highest-quality checkpoint published by Comfy-Org. New model variants can be added by extending `scripts/download_models.py` and rebuilding with a different `MODEL_TYPE`.

---

## Table of Contents

- [Features](#features)
- [Quickstart](#quickstart)
- [Test Your Endpoint](#test-your-endpoint)
- [Building the Docker Image](#building-the-docker-image)
- [API Specification](#api-specification)
- [Workflow ↔ MODEL_TYPE Compatibility](#workflow--model_type-compatibility)
- [Configuration](#configuration)
- [Local Development](#local-development)

---

## Features

- **High-quality music generation** — XL SFT (4B params) generates ~30–120 s clips at 48 kHz stereo
- **Lyrics support** — feed lyrics and a tag/style prompt; ACE-Step sings them
- **Cloudflare R2 output** — set bucket env vars to get presigned URLs back; otherwise inline base64
- **Stereo WAV output** — FLAC from ComfyUI is converted to 44.1 kHz stereo WAV before return
- **Build-time model selector** — `MODEL_TYPE` Docker arg controls which checkpoint set is baked in
- **No runtime tunables beyond R2** — every other knob is hard-coded to known-good values

## Quickstart

1. Build the Docker image on a Hetzner server (see [Building the Docker Image](#building-the-docker-image))
2. The build script pushes the image to Docker Hub
3. Create a RunPod serverless template pointing at the pushed image
4. Create a RunPod endpoint from the template (recommended: GPU with 16+ GB VRAM, e.g. A100 / H100 / RTX 4090)
5. POST `test_input.json` to your endpoint (see [Test Your Endpoint](#test-your-endpoint))

## Test Your Endpoint

The repo ships **one** copy-pasteable test payload: [`test_input.json`](./test_input.json) at the repo root. The full request body shape is `{"input": {"workflow": {...}}}` — do not strip the outer `{"input": ...}` wrapper or paste only the inner workflow nodes, or the worker will respond with `"Missing 'workflow' parameter"`.

### Curl

```bash
curl -X POST \
  -H "Authorization: Bearer <YOUR_RUNPOD_API_KEY>" \
  -H "Content-Type: application/json" \
  -d @test_input.json \
  https://api.runpod.ai/v2/<YOUR_ENDPOINT_ID>/runsync
```

### RunPod Dashboard

Open your endpoint → **Requests** tab → paste the entire contents of `test_input.json` into the Request JSON editor → Send. The dashboard expects the full `{"input": {...}}` envelope verbatim.

### Customizing the request

Edit `test_input.json` directly. The fields you typically want to tweak live inside `input.workflow`:

- Node `"5"`.`inputs.seconds` — duration in seconds (5–600)
- Node `"6"`.`inputs.tags` — genre/style prompt (e.g. `"lo-fi, jazz, mellow piano"`)
- Node `"6"`.`inputs.lyrics` — set to `""` for instrumental, or paste lyrics with `[Verse]` / `[Chorus]` markers
- Node `"6"`.`inputs.bpm` — beats per minute (10–300)
- Node `"6"`.`inputs.keyscale` — e.g. `"C major"`, `"A minor"`
- Node `"6"`.`inputs.duration` — keep equal to node 5's `seconds`
- Node `"6"`.`inputs.seed` and node `"8"`.`inputs.seed` — set to the same non-zero value for reproducible output

Expected response:

```json
{
  "id": "sync-<uuid>",
  "status": "COMPLETED",
  "output": {
    "audio": [
      {
        "filename": "ACEStep_00001_.wav",
        "type": "base64",
        "data": "UklGRiQAAABXQVZF..."
      }
    ]
  }
}
```

(Or `"type": "s3_url"` with a presigned R2 URL when the `BUCKET_*` env vars are set.)

## Building the Docker Image

### On a Hetzner Server (Recommended)

SSH into a Hetzner box (any CPX/CCX/dedicated with ≥80 GB free disk) and run:

```bash
export DOCKERHUB_USERNAME="your-dockerhub-user"
export DOCKERHUB_TOKEN="your-dockerhub-access-token"
export IMAGE_TAG="your-user/music-generator-runpod-worker:latest"

curl -fsSL https://raw.githubusercontent.com/Jmendapara/music-generator-runpod-worker/main/scripts/build-on-pod.sh | bash
```

The script installs Docker if missing, logs into Docker Hub, clones this repo, runs `docker buildx build --push` with `MODEL_TYPE=ace-step-1.5-xl-sft` baked in as the default, and prunes the box afterward.

### Manual Build

```bash
git clone https://github.com/Jmendapara/music-generator-runpod-worker.git
cd music-generator-runpod-worker

docker buildx build \
  --platform linux/amd64 \
  --target final \
  --build-arg MODEL_TYPE=ace-step-1.5-xl-sft \
  -t music-generator-runpod-worker:xl-sft .
```

### Model Variants

| `MODEL_TYPE` | Checkpoint | Approx. Image Size | Notes |
|---|---|---|---|
| `ace-step-1.5-xl-sft` *(default)* | `acestep_v1.5_xl_sft_bf16.safetensors` + `qwen_0.6b_ace15` + `qwen_4b_ace15` + `ace_1.5_vae` | ~24 GB models, ~30 GB total | Best quality. 50 steps, CFG 7, ~12 GB VRAM with offload. |

To add another variant: append an entry to `MODEL_SETS` in `scripts/download_models.py`, then rebuild with `--build-arg MODEL_TYPE=<new-key>`.

## API Specification

The worker exposes standard RunPod serverless endpoints (`/run`, `/runsync`, `/health`).

### Input

```json
{
  "input": {
    "workflow": { /* ComfyUI workflow JSON in API format */ },
    "images": [
      { "name": "reference.wav", "image": "data:audio/wav;base64,..." }
    ]
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `input.workflow` | Object | Yes | ComfyUI workflow in API format. See [Workflow ↔ MODEL_TYPE Compatibility](#workflow--model_type-compatibility) for what the baked-in variant accepts. |
| `input.images` | Array | No | Optional input files (uploaded via ComfyUI's `/upload/image`). Each object has `name` and `image` (base64 or data URI) keys. |

### Output

```json
{
  "id": "sync-uuid-string",
  "status": "COMPLETED",
  "output": {
    "audio": [
      {
        "filename": "ACEStep_00001_.wav",
        "type": "s3_url",
        "data": "https://<account>.r2.cloudflarestorage.com/..."
      }
    ]
  }
}
```

| Field | Type | Description |
|---|---|---|
| `output.audio` | Array | Audio files generated by the workflow |
| `output.images` | Array | Image files, if the workflow produces any (ACE-Step does not) |
| `output.errors` | Array | Non-fatal errors or warnings |

Each item in `output.audio` or `output.images`:

| Field | Type | Description |
|---|---|---|
| `filename` | String | Final filename (FLAC outputs are renamed to `.wav` after conversion) |
| `type` | String | `"base64"` or `"s3_url"` |
| `data` | String | Base64-encoded WAV bytes, or a presigned R2 URL |

## Workflow ↔ MODEL_TYPE Compatibility

**The worker is workflow-agnostic.** It loads whichever JSON you POST and submits it to ComfyUI as-is. But the workflow must reference model filenames that match what was baked into the image at build time.

For the default `MODEL_TYPE=ace-step-1.5-xl-sft`, the workflow must reference these exact filenames:

| Node | Field | Required Value |
|---|---|---|
| `UNETLoader` | `unet_name` | `acestep_v1.5_xl_sft_bf16.safetensors` |
| `DualCLIPLoader` | `clip_name1` | `qwen_0.6b_ace15.safetensors` |
| `DualCLIPLoader` | `clip_name2` | `qwen_4b_ace15.safetensors` |
| `DualCLIPLoader` | `type` | `ace` |
| `VAELoader` | `vae_name` | `ace_1.5_vae.safetensors` |

If you build the image with a different `MODEL_TYPE`, update the bundled workflow filenames to match, or you'll get a `ckpt_name not in list` validation error from ComfyUI. The handler will include the list of available checkpoints in the error response to help diagnose mismatches.

## Configuration

The worker exposes only four runtime environment variables — all related to Cloudflare R2 output upload. Everything else (logging, websocket retries, ComfyUI restart behavior) is hard-coded.

| Environment Variable | Required | Description |
|---|---|---|
| `BUCKET_ENDPOINT_URL` | No | Cloudflare R2 endpoint (e.g. `https://<account>.r2.cloudflarestorage.com`). When set, outputs are uploaded to R2 and returned as `type: "s3_url"`. When unset, outputs are returned as `type: "base64"`. |
| `BUCKET_ACCESS_KEY_ID` | No | R2 access key |
| `BUCKET_SECRET_ACCESS_KEY` | No | R2 secret |
| `BUCKET_NAME` | No | R2 bucket name. Defaults to `month-year`. |

## Local Development

```bash
# Build the development image
docker build --target final --build-arg MODEL_TYPE=ace-step-1.5-xl-sft -t music-generator-runpod-worker:dev .

# Run with docker-compose
docker-compose up

# ComfyUI UI: http://localhost:8188
# RunPod API: http://localhost:8000
```

## Credits

- [ACE-Step](https://github.com/ace-step/ACE-Step) — original music generation model
- [ACE-Step 1.5 XL SFT (Comfy-Org repackage)](https://huggingface.co/Comfy-Org/ace_step_1.5_ComfyUI_files) — checkpoints repackaged for ComfyUI
- [ComfyUI](https://github.com/comfyanonymous/ComfyUI) — execution engine
- [RunPod worker-comfyui](https://github.com/runpod-workers/worker-comfyui) — base handler architecture
