"""
Download model weights for the music-generator-runpod-worker.

Reads MODEL_TYPE from the env var of the same name, looks up the file set in
MODEL_SETS, downloads each file from Hugging Face via hf_hub_download, and
moves it into the ComfyUI models directory layout.

Add new model variants by appending a key to MODEL_SETS.
"""

import os
import shutil
import sys

from huggingface_hub import hf_hub_download


MODEL_SETS = {
    "ace-step-1.5-xl-sft": [
        (
            "Comfy-Org/ace_step_1.5_ComfyUI_files",
            "split_files/diffusion_models/acestep_v1.5_xl_sft_bf16.safetensors",
            "/comfyui/models/diffusion_models/acestep_v1.5_xl_sft_bf16.safetensors",
        ),
        (
            "Comfy-Org/ace_step_1.5_ComfyUI_files",
            "split_files/text_encoders/qwen_0.6b_ace15.safetensors",
            "/comfyui/models/text_encoders/qwen_0.6b_ace15.safetensors",
        ),
        (
            "Comfy-Org/ace_step_1.5_ComfyUI_files",
            "split_files/text_encoders/qwen_4b_ace15.safetensors",
            "/comfyui/models/text_encoders/qwen_4b_ace15.safetensors",
        ),
        (
            "Comfy-Org/ace_step_1.5_ComfyUI_files",
            "split_files/vae/ace_1.5_vae.safetensors",
            "/comfyui/models/vae/ace_1.5_vae.safetensors",
        ),
    ],
}


def main() -> int:
    model_type = os.environ.get("MODEL_TYPE", "").strip()
    if not model_type:
        print("ERROR: MODEL_TYPE env var is not set", file=sys.stderr)
        return 1

    if model_type not in MODEL_SETS:
        print(
            f"ERROR: Unknown MODEL_TYPE '{model_type}'. Known values: {sorted(MODEL_SETS)}",
            file=sys.stderr,
        )
        return 1

    token = os.environ.get("HUGGINGFACE_ACCESS_TOKEN") or None
    print(f"Downloading model set for MODEL_TYPE={model_type}")

    for repo_id, repo_filename, dest in MODEL_SETS[model_type]:
        print(f"  {repo_id}:{repo_filename} -> {dest}")
        src = hf_hub_download(
            repo_id=repo_id,
            filename=repo_filename,
            local_dir="/tmp/hf_download",
            token=token,
        )
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(src, dest)
        print(f"    OK ({os.path.getsize(dest):,} bytes)")

    print("All models downloaded.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
