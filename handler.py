"""
Music Generator (ACE-Step) RunPod Worker.

Wraps ComfyUI with an ACE-Step 1.5 model baked into the image and exposes
the standard worker-comfyui contract: take a workflow JSON in the request,
queue it via the ComfyUI HTTP API, stream progress over the local WebSocket,
fetch outputs from /view, convert FLAC audio to WAV, and return the result
either as base64 inline or as a Cloudflare R2 URL when bucket env vars
are configured.
"""

import runpod
import json
import urllib.request
import urllib.parse
import time
import os
import requests
import base64
from io import BytesIO
import websocket
import uuid
import tempfile
import socket
import traceback
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

COMFY_API_AVAILABLE_INTERVAL_MS = 50
COMFY_API_AVAILABLE_MAX_RETRIES = 500
WEBSOCKET_RECONNECT_ATTEMPTS = 5
WEBSOCKET_RECONNECT_DELAY_S = 3

COMFY_HOST = "127.0.0.1:8188"


def _make_s3_client():
    """Create a boto3 S3 client pointing at the configured R2 endpoint.

    Cloudflare R2 only accepts SigV4-signed requests, so we pin signature_version
    here. region_name='auto' is the R2-recommended placeholder (R2 is region-less
    but boto3 requires the field to be set).
    """
    import boto3
    from botocore.config import Config

    endpoint = os.environ.get("BUCKET_ENDPOINT_URL")
    access_key = os.environ.get("BUCKET_ACCESS_KEY_ID")
    secret_key = os.environ.get("BUCKET_SECRET_ACCESS_KEY")
    if not endpoint or not access_key or not secret_key:
        raise ValueError(
            "R2 credentials not configured. BUCKET_ENDPOINT_URL, BUCKET_ACCESS_KEY_ID, "
            "and BUCKET_SECRET_ACCESS_KEY must all be set to upload outputs to R2."
        )
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
        config=Config(signature_version="s3v4"),
    )


def _guess_content_type(ext):
    ext = ext.lower()
    return {
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
    }.get(ext, "application/octet-stream")


def upload_output_to_r2(file_bytes, filename, job_id, uid=None):
    """
    Upload output bytes to R2 and return a 7-day presigned URL.

    Leaf: <8-char-uuid>.<ext> — collision-free.

    Prefix:
      - if uid is provided: users/<uid>/generations/
      - otherwise:          <job_id>/
    """
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not bucket:
        raise ValueError("R2_BUCKET_NAME must be set to upload outputs to R2.")

    ext = os.path.splitext(filename)[1] or ".wav"
    leaf = f"{str(uuid.uuid4())[:8]}{ext}"
    if uid:
        key = f"users/{uid}/generations/{leaf}"
    else:
        key = f"{job_id}/{leaf}"

    s3 = _make_s3_client()
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=file_bytes,
        ContentType=_guess_content_type(ext),
    )
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=604800,  # 7 days
    )


def _comfy_server_status():
    """Return a dictionary with basic reachability info for the ComfyUI HTTP server."""
    try:
        resp = requests.get(f"http://{COMFY_HOST}/", timeout=5)
        return {
            "reachable": resp.status_code == 200,
            "status_code": resp.status_code,
        }
    except Exception as exc:
        return {"reachable": False, "error": str(exc)}


def _attempt_websocket_reconnect(ws_url, max_attempts, delay_s, initial_error):
    """
    Attempts to reconnect to the WebSocket server after a disconnect.

    Returns a newly connected WebSocket object, or raises
    WebSocketConnectionClosedException if all attempts fail.
    """
    print(
        f"worker-comfyui - Websocket connection closed unexpectedly: {initial_error}. Attempting to reconnect..."
    )
    last_reconnect_error = initial_error
    for attempt in range(max_attempts):
        srv_status = _comfy_server_status()
        if not srv_status["reachable"]:
            print(
                f"worker-comfyui - ComfyUI HTTP unreachable – aborting websocket reconnect: {srv_status.get('error', 'status '+str(srv_status.get('status_code')))}"
            )
            raise websocket.WebSocketConnectionClosedException(
                "ComfyUI HTTP unreachable during websocket reconnect"
            )

        print(
            f"worker-comfyui - Reconnect attempt {attempt + 1}/{max_attempts}... (ComfyUI HTTP reachable, status {srv_status.get('status_code')})"
        )
        try:
            new_ws = websocket.WebSocket()
            new_ws.connect(ws_url, timeout=10)
            print(f"worker-comfyui - Websocket reconnected successfully.")
            return new_ws
        except (
            websocket.WebSocketException,
            ConnectionRefusedError,
            socket.timeout,
            OSError,
        ) as reconn_err:
            last_reconnect_error = reconn_err
            print(
                f"worker-comfyui - Reconnect attempt {attempt + 1} failed: {reconn_err}"
            )
            if attempt < max_attempts - 1:
                print(
                    f"worker-comfyui - Waiting {delay_s} seconds before next attempt..."
                )
                time.sleep(delay_s)
            else:
                print(f"worker-comfyui - Max reconnection attempts reached.")

    print("worker-comfyui - Failed to reconnect websocket after connection closed.")
    raise websocket.WebSocketConnectionClosedException(
        f"Connection closed and failed to reconnect. Last error: {last_reconnect_error}"
    )


def validate_input(job_input):
    """
    Validates the input for the handler function.

    Returns (validated_data, error_message).
    """
    if job_input is None:
        return None, "Please provide input"

    if isinstance(job_input, str):
        try:
            job_input = json.loads(job_input)
        except json.JSONDecodeError:
            return None, "Invalid JSON format in input"

    workflow = job_input.get("workflow")
    if workflow is None:
        return None, "Missing 'workflow' parameter"

    images = job_input.get("images")
    if images is not None:
        if not isinstance(images, list) or not all(
            "name" in image and "image" in image for image in images
        ):
            return (
                None,
                "'images' must be a list of objects with 'name' and 'image' keys",
            )

    uid = job_input.get("uid")
    if uid is not None:
        if not isinstance(uid, str) or not uid.strip():
            return None, "'uid' must be a non-empty string"
        if "/" in uid:
            return None, "'uid' must not contain '/'"

    return {
        "workflow": workflow,
        "images": images,
        "uid": uid,
    }, None


def check_server(url, retries=500, delay=50):
    """
    Check if a server is reachable via HTTP GET request.

    Args:
        url: The URL to check
        retries: Number of attempts
        delay: Time in milliseconds between retries
    """
    print(f"worker-comfyui - Checking API server at {url}...")
    for i in range(retries):
        try:
            response = requests.get(url, timeout=5)
            if response.status_code == 200:
                print(f"worker-comfyui - API is reachable")
                return True
        except requests.Timeout:
            pass
        except requests.RequestException:
            pass

        time.sleep(delay / 1000)

    print(
        f"worker-comfyui - Failed to connect to server at {url} after {retries} attempts."
    )
    return False


def upload_images(images):
    """
    Upload a list of base64 encoded images or audio files to ComfyUI via /upload/image.
    """
    if not images:
        return {"status": "success", "message": "No images to upload", "details": []}

    responses = []
    upload_errors = []

    print(f"worker-comfyui - Uploading {len(images)} file(s)...")

    for image in images:
        try:
            name = image["name"]
            image_data_uri = image["image"]

            if "," in image_data_uri:
                base64_data = image_data_uri.split(",", 1)[1]
            else:
                base64_data = image_data_uri

            blob = base64.b64decode(base64_data)

            ext = os.path.splitext(name)[1].lower()
            content_types = {
                ".png": "image/png",
                ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg",
                ".webp": "image/webp",
                ".wav": "audio/wav",
                ".mp3": "audio/mpeg",
                ".flac": "audio/flac",
                ".ogg": "audio/ogg",
            }
            content_type = content_types.get(ext, "application/octet-stream")

            files = {
                "image": (name, BytesIO(blob), content_type),
                "overwrite": (None, "true"),
            }

            response = requests.post(
                f"http://{COMFY_HOST}/upload/image", files=files, timeout=30
            )
            response.raise_for_status()

            responses.append(f"Successfully uploaded {name}")
            print(f"worker-comfyui - Successfully uploaded {name}")

        except base64.binascii.Error as e:
            error_msg = f"Error decoding base64 for {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.Timeout:
            error_msg = f"Timeout uploading {image.get('name', 'unknown')}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except requests.RequestException as e:
            error_msg = f"Error uploading {image.get('name', 'unknown')}: {e}"
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)
        except Exception as e:
            error_msg = (
                f"Unexpected error uploading {image.get('name', 'unknown')}: {e}"
            )
            print(f"worker-comfyui - {error_msg}")
            upload_errors.append(error_msg)

    if upload_errors:
        print(f"worker-comfyui - file(s) upload finished with errors")
        return {
            "status": "error",
            "message": "Some files failed to upload",
            "details": upload_errors,
        }

    print(f"worker-comfyui - file(s) upload complete")
    return {
        "status": "success",
        "message": "All files uploaded successfully",
        "details": responses,
    }


def get_available_models():
    """Get list of available models from ComfyUI."""
    try:
        response = requests.get(f"http://{COMFY_HOST}/object_info", timeout=10)
        response.raise_for_status()
        object_info = response.json()

        available_models = {}
        if "CheckpointLoaderSimple" in object_info:
            checkpoint_info = object_info["CheckpointLoaderSimple"]
            if "input" in checkpoint_info and "required" in checkpoint_info["input"]:
                ckpt_options = checkpoint_info["input"]["required"].get("ckpt_name")
                if ckpt_options and len(ckpt_options) > 0:
                    available_models["checkpoints"] = (
                        ckpt_options[0] if isinstance(ckpt_options[0], list) else []
                    )

        return available_models
    except Exception as e:
        print(f"worker-comfyui - Warning: Could not fetch available models: {e}")
        return {}


def queue_workflow(workflow, client_id):
    """
    Queue a workflow to be processed by ComfyUI.

    Raises ValueError if the workflow validation fails.
    """
    payload = {"prompt": workflow, "client_id": client_id}
    data = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    response = requests.post(
        f"http://{COMFY_HOST}/prompt", data=data, headers=headers, timeout=30
    )

    if response.status_code == 400:
        print(f"worker-comfyui - ComfyUI returned 400. Response body: {response.text}")
        try:
            error_data = response.json()
            print(f"worker-comfyui - Parsed error data: {error_data}")

            error_message = "Workflow validation failed"
            error_details = []

            if "error" in error_data:
                error_info = error_data["error"]
                if isinstance(error_info, dict):
                    error_message = error_info.get("message", error_message)
                    if error_info.get("type") == "prompt_outputs_failed_validation":
                        error_message = "Workflow validation failed"
                else:
                    error_message = str(error_info)

            if "node_errors" in error_data:
                for node_id, node_error in error_data["node_errors"].items():
                    if isinstance(node_error, dict):
                        for error_type, error_msg in node_error.items():
                            error_details.append(
                                f"Node {node_id} ({error_type}): {error_msg}"
                            )
                    else:
                        error_details.append(f"Node {node_id}: {node_error}")

            if error_data.get("type") == "prompt_outputs_failed_validation":
                error_message = error_data.get("message", "Workflow validation failed")
                available_models = get_available_models()
                if available_models.get("checkpoints"):
                    error_message += f"\n\nThis usually means a required model or parameter is not available."
                    error_message += f"\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                else:
                    error_message += "\n\nThis usually means a required model or parameter is not available."
                    error_message += "\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(error_message)

            if error_details:
                detailed_message = f"{error_message}:\n" + "\n".join(
                    f"• {detail}" for detail in error_details
                )

                if any(
                    "not in list" in detail and "ckpt_name" in detail
                    for detail in error_details
                ):
                    available_models = get_available_models()
                    if available_models.get("checkpoints"):
                        detailed_message += f"\n\nAvailable checkpoint models: {', '.join(available_models['checkpoints'])}"
                    else:
                        detailed_message += "\n\nNo checkpoint models appear to be available. Please check your model installation."

                raise ValueError(detailed_message)
            else:
                raise ValueError(f"{error_message}. Raw response: {response.text}")

        except (json.JSONDecodeError, KeyError):
            raise ValueError(
                f"ComfyUI validation failed (could not parse error response): {response.text}"
            )

    response.raise_for_status()
    return response.json()


def get_history(prompt_id):
    """Retrieve the history of a given prompt using its ID."""
    response = requests.get(f"http://{COMFY_HOST}/history/{prompt_id}", timeout=30)
    response.raise_for_status()
    return response.json()


def get_file_data(filename, subfolder, file_type):
    """
    Fetch file bytes (image or audio) from the ComfyUI /view endpoint.

    Returns bytes or None on error.
    """
    print(
        f"worker-comfyui - Fetching file data: type={file_type}, subfolder={subfolder}, filename={filename}"
    )
    data = {"filename": filename, "subfolder": subfolder, "type": file_type}
    url_values = urllib.parse.urlencode(data)
    try:
        response = requests.get(f"http://{COMFY_HOST}/view?{url_values}", timeout=60)
        response.raise_for_status()
        print(f"worker-comfyui - Successfully fetched file data for {filename}")
        return response.content
    except requests.Timeout:
        print(f"worker-comfyui - Timeout fetching file data for {filename}")
        return None
    except requests.RequestException as e:
        print(f"worker-comfyui - Error fetching file data for {filename}: {e}")
        return None
    except Exception as e:
        print(
            f"worker-comfyui - Unexpected error fetching file data for {filename}: {e}"
        )
        return None


def _convert_flac_to_wav(file_bytes, filename):
    """
    Convert FLAC audio bytes to stereo 44.1 kHz WAV using ffmpeg.
    Returns (wav_bytes, new_filename) or (original_bytes, original_filename) on failure.
    """
    import subprocess

    flac_path = None
    wav_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".flac", delete=False) as flac_tmp:
            flac_tmp.write(file_bytes)
            flac_path = flac_tmp.name

        wav_path = flac_path.replace(".flac", ".wav")
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", flac_path, "-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", wav_path],
            capture_output=True, timeout=60,
        )

        os.remove(flac_path)
        flac_path = None

        if result.returncode != 0:
            stderr_text = result.stderr.decode("utf-8", errors="replace")[-500:]
            print(f"worker-comfyui - ffmpeg FLAC→WAV failed (code {result.returncode}): {stderr_text}")
            if wav_path and os.path.exists(wav_path):
                os.remove(wav_path)
            return file_bytes, filename

        with open(wav_path, "rb") as f:
            wav_bytes = f.read()
        os.remove(wav_path)
        wav_path = None

        new_filename = os.path.splitext(filename)[0] + ".wav"
        print(f"worker-comfyui - Converted {filename} FLAC→WAV ({len(file_bytes)} → {len(wav_bytes)} bytes)")
        return wav_bytes, new_filename

    except Exception as e:
        print(f"worker-comfyui - FLAC→WAV conversion error: {e}")
        for p in (flac_path, wav_path):
            if p and os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        return file_bytes, filename


def _process_output_file(filename, subfolder, file_type, job_id, output_list, errors, uid=None):
    """
    Fetch a single output file from ComfyUI and append it to output_list
    as either base64 data or an S3 URL. FLAC audio is converted to WAV first.

    When uid is provided and R2 env vars are set, the object is keyed at
    users/<uid>/generations/<uuid8>.<ext>; otherwise it falls back to
    <job_id>/<uuid8>.<ext>.
    """
    if file_type == "temp":
        print(f"worker-comfyui - Skipping {filename} because type is 'temp'")
        return

    if not filename:
        warn_msg = f"Skipping file due to missing filename"
        print(f"worker-comfyui - {warn_msg}")
        errors.append(warn_msg)
        return

    file_bytes = get_file_data(filename, subfolder, file_type)

    if file_bytes:
        file_extension = os.path.splitext(filename)[1] or ".bin"

        if file_extension.lower() == ".flac":
            file_bytes, filename = _convert_flac_to_wav(file_bytes, filename)

        if os.environ.get("BUCKET_ENDPOINT_URL"):
            try:
                print(f"worker-comfyui - Uploading {filename} to R2 (uid={uid or 'none'})...")
                s3_url = upload_output_to_r2(file_bytes, filename, job_id, uid=uid)
                print(f"worker-comfyui - Uploaded {filename} to R2: {s3_url}")
                output_list.append(
                    {
                        "filename": filename,
                        "type": "s3_url",
                        "data": s3_url,
                    }
                )
            except Exception as e:
                error_msg = f"Error uploading {filename} to R2: {e}"
                print(f"worker-comfyui - {error_msg}")
                errors.append(error_msg)
        else:
            try:
                base64_data = base64.b64encode(file_bytes).decode("utf-8")
                output_list.append(
                    {
                        "filename": filename,
                        "type": "base64",
                        "data": base64_data,
                    }
                )
                print(f"worker-comfyui - Encoded {filename} as base64")
            except Exception as e:
                error_msg = f"Error encoding {filename} to base64: {e}"
                print(f"worker-comfyui - {error_msg}")
                errors.append(error_msg)
    else:
        error_msg = f"Failed to fetch file data for {filename} from /view endpoint."
        errors.append(error_msg)


def handler(job):
    """
    Handles a RunPod job using ComfyUI via websockets.

    Supports both audio and image output from ComfyUI workflows.
    """
    job_input = job["input"]
    job_id = job["id"]

    validated_data, error_message = validate_input(job_input)
    if error_message:
        return {"error": error_message}

    workflow = validated_data["workflow"]
    input_images = validated_data.get("images")
    uid = validated_data.get("uid")

    if not check_server(
        f"http://{COMFY_HOST}/",
        COMFY_API_AVAILABLE_MAX_RETRIES,
        COMFY_API_AVAILABLE_INTERVAL_MS,
    ):
        return {
            "error": f"ComfyUI server ({COMFY_HOST}) not reachable after multiple retries."
        }

    if input_images:
        upload_result = upload_images(input_images)
        if upload_result["status"] == "error":
            return {
                "error": "Failed to upload one or more input files",
                "details": upload_result["details"],
            }

    ws = None
    client_id = str(uuid.uuid4())
    prompt_id = None
    output_images = []
    output_audio = []
    errors = []

    try:
        ws_url = f"ws://{COMFY_HOST}/ws?clientId={client_id}"
        print(f"worker-comfyui - Connecting to websocket: {ws_url}")
        ws = websocket.WebSocket()
        ws.connect(ws_url, timeout=10)
        print(f"worker-comfyui - Websocket connected")

        try:
            queued_workflow = queue_workflow(workflow, client_id)
            prompt_id = queued_workflow.get("prompt_id")
            if not prompt_id:
                raise ValueError(
                    f"Missing 'prompt_id' in queue response: {queued_workflow}"
                )
            print(f"worker-comfyui - Queued workflow with ID: {prompt_id}")
        except requests.RequestException as e:
            print(f"worker-comfyui - Error queuing workflow: {e}")
            raise ValueError(f"Error queuing workflow: {e}")
        except Exception as e:
            print(f"worker-comfyui - Unexpected error queuing workflow: {e}")
            if isinstance(e, ValueError):
                raise e
            else:
                raise ValueError(f"Unexpected error queuing workflow: {e}")

        print(f"worker-comfyui - Waiting for workflow execution ({prompt_id})...")
        execution_done = False
        while True:
            try:
                out = ws.recv()
                if isinstance(out, str):
                    message = json.loads(out)
                    if message.get("type") == "status":
                        status_data = message.get("data", {}).get("status", {})
                        print(
                            f"worker-comfyui - Status update: {status_data.get('exec_info', {}).get('queue_remaining', 'N/A')} items remaining in queue"
                        )
                    elif message.get("type") == "executing":
                        data = message.get("data", {})
                        if (
                            data.get("node") is None
                            and data.get("prompt_id") == prompt_id
                        ):
                            print(
                                f"worker-comfyui - Execution finished for prompt {prompt_id}"
                            )
                            execution_done = True
                            break
                    elif message.get("type") == "execution_error":
                        data = message.get("data", {})
                        if data.get("prompt_id") == prompt_id:
                            error_details = f"Node Type: {data.get('node_type')}, Node ID: {data.get('node_id')}, Message: {data.get('exception_message')}"
                            print(
                                f"worker-comfyui - Execution error received: {error_details}"
                            )
                            errors.append(f"Workflow execution error: {error_details}")
                            break
                else:
                    continue
            except websocket.WebSocketTimeoutException:
                print(f"worker-comfyui - Websocket receive timed out. Still waiting...")
                continue
            except websocket.WebSocketConnectionClosedException as closed_err:
                try:
                    ws = _attempt_websocket_reconnect(
                        ws_url,
                        WEBSOCKET_RECONNECT_ATTEMPTS,
                        WEBSOCKET_RECONNECT_DELAY_S,
                        closed_err,
                    )
                    print(
                        "worker-comfyui - Resuming message listening after successful reconnect."
                    )
                    continue
                except (
                    websocket.WebSocketConnectionClosedException
                ) as reconn_failed_err:
                    raise reconn_failed_err

            except json.JSONDecodeError:
                print(f"worker-comfyui - Received invalid JSON message via websocket.")

        if not execution_done and not errors:
            raise ValueError(
                "Workflow monitoring loop exited without confirmation of completion or error."
            )

        print(f"worker-comfyui - Fetching history for prompt {prompt_id}...")
        history = get_history(prompt_id)

        if prompt_id not in history:
            error_msg = f"Prompt ID {prompt_id} not found in history after execution."
            print(f"worker-comfyui - {error_msg}")
            if not errors:
                return {"error": error_msg}
            else:
                errors.append(error_msg)
                return {
                    "error": "Job processing failed, prompt ID not found in history.",
                    "details": errors,
                }

        prompt_history = history.get(prompt_id, {})
        outputs = prompt_history.get("outputs", {})

        if not outputs:
            warning_msg = f"No outputs found in history for prompt {prompt_id}."
            print(f"worker-comfyui - {warning_msg}")
            if not errors:
                errors.append(warning_msg)

        print(f"worker-comfyui - Processing {len(outputs)} output nodes...")
        for node_id, node_output in outputs.items():
            if "images" in node_output:
                print(
                    f"worker-comfyui - Node {node_id} contains {len(node_output['images'])} image(s)"
                )
                for image_info in node_output["images"]:
                    _process_output_file(
                        filename=image_info.get("filename"),
                        subfolder=image_info.get("subfolder", ""),
                        file_type=image_info.get("type"),
                        job_id=job_id,
                        output_list=output_images,
                        errors=errors,
                        uid=uid,
                    )

            if "audio" in node_output:
                print(
                    f"worker-comfyui - Node {node_id} contains {len(node_output['audio'])} audio file(s)"
                )
                for audio_info in node_output["audio"]:
                    _process_output_file(
                        filename=audio_info.get("filename"),
                        subfolder=audio_info.get("subfolder", ""),
                        file_type=audio_info.get("type"),
                        job_id=job_id,
                        output_list=output_audio,
                        errors=errors,
                        uid=uid,
                    )

            handled_keys = {"images", "audio"}
            other_keys = [k for k in node_output.keys() if k not in handled_keys]
            if other_keys:
                warn_msg = (
                    f"Node {node_id} produced unhandled output keys: {other_keys}."
                )
                print(f"worker-comfyui - WARNING: {warn_msg}")

    except websocket.WebSocketException as e:
        print(f"worker-comfyui - WebSocket Error: {e}")
        print(traceback.format_exc())
        return {"error": f"WebSocket communication error: {e}"}
    except requests.RequestException as e:
        print(f"worker-comfyui - HTTP Request Error: {e}")
        print(traceback.format_exc())
        return {"error": f"HTTP communication error with ComfyUI: {e}"}
    except ValueError as e:
        print(f"worker-comfyui - Value Error: {e}")
        print(traceback.format_exc())
        return {"error": str(e)}
    except Exception as e:
        print(f"worker-comfyui - Unexpected Handler Error: {e}")
        print(traceback.format_exc())
        return {"error": f"An unexpected error occurred: {e}"}
    finally:
        if ws and ws.connected:
            print(f"worker-comfyui - Closing websocket connection.")
            ws.close()

    final_result = {}

    if output_images:
        final_result["images"] = output_images

    if output_audio:
        final_result["audio"] = output_audio

    if errors:
        final_result["errors"] = errors
        print(f"worker-comfyui - Job completed with errors/warnings: {errors}")

    has_output = output_images or output_audio

    if not has_output and errors:
        print(f"worker-comfyui - Job failed with no output.")
        return {
            "error": "Job processing failed",
            "details": errors,
        }
    elif not has_output and not errors:
        print(
            f"worker-comfyui - Job completed successfully, but the workflow produced no output."
        )
        final_result["status"] = "success_no_output"
        final_result["images"] = []
        final_result["audio"] = []

    print(
        f"worker-comfyui - Job completed. Returning {len(output_images)} image(s) and {len(output_audio)} audio file(s)."
    )
    return final_result


if __name__ == "__main__":
    print("worker-comfyui - Starting handler...")
    runpod.serverless.start({"handler": handler})
