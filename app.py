
import os
import re
import uuid
import time
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app, origins=os.environ.get("ALLOWED_ORIGIN", "").split(","))

API_KEY = os.environ.get("API_KEY", "")
CLIP_DIR = Path("/tmp/clips")
CLIP_DIR.mkdir(parents=True, exist_ok=True)

MAX_CLIP_SECONDS = 600
PROCESS_TIMEOUT = 600
FORMATS = {"mp4", "mp3", "webm", "m4a"}
QUALITIES = {"360", "480", "720", "1080", "1440", "2160"}

def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("Time must be text.")
    parts = value.strip().split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("Use HH:MM:SS or MM:SS.")
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        raise ValueError("Invalid time.")
    if any(n < 0 for n in nums):
        raise ValueError("Time cannot be negative.")
    if len(nums) == 3:
        if nums[1] >= 60 or nums[2] >= 60:
            raise ValueError("Invalid time.")
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("Invalid time.")
        return nums[0] * 60 + nums[1]
    return nums[0]

def valid_youtube_url(url):
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        allowed = {
            "youtube.com", "www.youtube.com", "m.youtube.com",
            "music.youtube.com", "youtu.be", "youtube-nocookie.com",
            "www.youtube-nocookie.com"
        }
        return parsed.scheme == "https" and host in allowed
    except Exception:
        return False

def require_key():
    supplied = request.headers.get("X-API-Key", "")
    return bool(API_KEY) and supplied == API_KEY

@app.get("/")
def home():
    return jsonify({
        "service": "Personal YouTube Clip Backend",
        "status": "online",
        "endpoints": ["/health", "/api/clip", "/download/<filename>"]
    })

@app.get("/health")
def health():
    return jsonify({"status": "ok"})

@app.post("/api/clip")
def create_clip():
    if not require_key():
        return jsonify({"error": "Unauthorized. Check API key."}), 401

    data = request.get_json(silent=True) or {}
    url = str(data.get("url", "")).strip()
    file_type = str(data.get("format", "mp4")).lower()
    quality = str(data.get("quality", "720"))

    if not valid_youtube_url(url):
        return jsonify({"error": "Enter a valid HTTPS YouTube URL."}), 400

    if file_type not in FORMATS:
        return jsonify({"error": "Unsupported file format."}), 400

    if quality not in QUALITIES:
        return jsonify({"error": "Unsupported quality."}), 400

    try:
        start = parse_time(data.get("start", "00:00:00"))
        end = parse_time(data.get("end", "00:01:00"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if end <= start:
        return jsonify({"error": "End time must be after start time."}), 400

    if end - start > MAX_CLIP_SECONDS:
        return jsonify({"error": "Free server limit: clips up to 10 minutes."}), 400

    job_id = uuid.uuid4().hex
    output_template = str(CLIP_DIR / f"{job_id}.%(ext)s")
    section = f"*{start}-{end}"

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--download-sections", section,
        "--force-overwrites",
        "--output", output_template,
        "--retries", "1",
        "--fragment-retries", "1",
    ]

    if file_type in {"mp3", "m4a"}:
        audio_format = "mp3" if file_type == "mp3" else "m4a"
        cmd += [
            "-f", "bestaudio/best",
            "-x", "--audio-format", audio_format,
        ]
    elif file_type == "webm":
        cmd += [
            "-f",
            f"bv*[height<={quality}][ext=webm]+ba[ext=webm]/b[ext=webm]/b",
            "--merge-output-format", "webm",
        ]
    else:
        cmd += [
            "-f",
            f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={quality}][ext=mp4]/b",
            "--merge-output-format", "mp4",
        ]

    cmd.append(url)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=PROCESS_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return jsonify({
            "error": "Processing timed out. Try a shorter clip or lower quality."
        }), 504
    except Exception:
        app.logger.exception("Processing failed")
        return jsonify({"error": "Server could not start processing."}), 500

    if result.returncode != 0:
        app.logger.warning("yt-dlp failed: %s", result.stderr[-2500:])
        return jsonify({
            "error": "Could not process this video. It may be unavailable, "
                     "unsupported, live, or restricted."
        }), 422

    candidates = list(CLIP_DIR.glob(f"{job_id}.*"))
    candidates = [
        p for p in candidates
        if p.is_file() and p.suffix.lower() in {
            ".mp4", ".mp3", ".webm", ".m4a"
        }
    ]

    if not candidates:
        return jsonify({"error": "Output file was not created."}), 500

    output_file = candidates[0]
    return jsonify({
        "message": "Clip is ready.",
        "format": output_file.suffix.lower().lstrip("."),
        "filename": output_file.name,
        "download_url": f"/download/{output_file.name}"
    })

@app.get("/download/<filename>")
def download_file(filename):
    # Only allow generated UUID filenames and supported extensions.
    if not re.fullmatch(
        r"[0-9a-f]{32}\.(mp4|mp3|webm|m4a)", filename
    ):
        return jsonify({"error": "Invalid filename."}), 400

    path = CLIP_DIR / filename
    if not path.is_file():
        return jsonify({"error": "File not found. It may have expired."}), 404

    return send_from_directory(
        str(CLIP_DIR),
        filename,
        as_attachment=True,
        download_name=filename,
        conditional=True,
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))

