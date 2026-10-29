
import os
import re
import hmac
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

API_KEY = os.environ.get("API_KEY", "")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "").strip().rstrip("/")

# केवल अपने Blogger origin को अनुमति दें।
if ALLOWED_ORIGIN:
    CORS(
        app,
        resources={
            r"/api/*": {
                "origins": [ALLOWED_ORIGIN],
                "allow_headers": ["Content-Type", "X-API-Key"],
                "methods": ["POST", "OPTIONS"],
            }
        },
    )

CLIP_DIR = Path("/tmp/clips")
CLIP_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(CLIP_DIR, 0o700)

MAX_CLIP_SECONDS = 600
MAX_PROCESS_SECONDS = 600
FILE_TTL_SECONDS = 2 * 60 * 60
MAX_REQUESTS_PER_HOUR = 10

ALLOWED_FORMATS = {"mp4", "mp3", "webm", "m4a"}
ALLOWED_QUALITIES = {"360", "480", "720", "1080", "1440", "2160"}

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "youtube-nocookie.com",
    "www.youtube-nocookie.com",
}

request_times = {}
rate_lock = threading.Lock()
job_lock = threading.Lock()


def parse_time(value):
    if not isinstance(value, str):
        raise ValueError("समय HH:MM:SS या MM:SS में लिखें।")

    parts = value.strip().split(":")

    if not 1 <= len(parts) <= 3:
        raise ValueError("समय का फॉर्मेट सही नहीं है।")

    if not all(part.isdigit() for part in parts):
        raise ValueError("समय में केवल अंक और कोलन इस्तेमाल करें।")

    nums = [int(part) for part in parts]

    if any(n < 0 for n in nums):
        raise ValueError("समय गलत है।")

    if len(nums) == 3:
        if nums[1] >= 60 or nums[2] >= 60:
            raise ValueError("समय का फॉर्मेट सही नहीं है।")
        return nums[0] * 3600 + nums[1] * 60 + nums[2]

    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("समय का फॉर्मेट सही नहीं है।")
        return nums[0] * 60 + nums[1]

    return nums[0]


def valid_youtube_url(url):
    try:
        parsed = urlparse(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in ALLOWED_HOSTS
            and parsed.username is None
            and parsed.password is None
            and parsed.port in (None, 443)
        )
    except (ValueError, TypeError):
        return False


def authorized():
    if not API_KEY:
        return False

    supplied = request.headers.get("X-API-Key", "")

    return (
        len(supplied) <= 256
        and hmac.compare_digest(supplied, API_KEY)
    )


def rate_limited():
    # Direct remote_addr इस्तेमाल करें; client-supplied forwarded
    # headers पर भरोसा न करें।
    client = request.remote_addr or "unknown"
    now = time.time()

    with rate_lock:
        recent = [
            stamp for stamp in request_times.get(client, [])
            if now - stamp < 3600
        ]

        if len(recent) >= MAX_REQUESTS_PER_HOUR:
            request_times[client] = recent
            return True

        recent.append(now)
        request_times[client] = recent

        # पुरानी rate-limit entries हटाएँ।
        for ip in list(request_times):
            request_times[ip] = [
                stamp for stamp in request_times[ip]
                if now - stamp < 3600
            ]
            if not request_times[ip]:
                del request_times[ip]

    return False


def cleanup_old_files():
    now = time.time()

    try:
        for path in CLIP_DIR.iterdir():
            try:
                # हर job का अपना अलग डायरेक्टरी होता है।
                if path.is_dir() and not path.is_symlink():
                    files = list(path.rglob("*"))
                    timestamps = [
                        item.stat().st_mtime
                        for item in files
                        if item.is_file() and not item.is_symlink()
                    ]
                    last_modified = max(
                        timestamps + [path.stat().st_mtime]
                    )

                    if now - last_modified >= FILE_TTL_SECONDS:
                        shutil.rmtree(path, ignore_errors=True)

                elif path.is_file() and not path.is_symlink():
                    if now - path.stat().st_mtime >= FILE_TTL_SECONDS:
                        path.unlink(missing_ok=True)

            except OSError:
                app.logger.warning("Temporary-file cleanup failed.")

    except OSError:
        app.logger.warning("Could not inspect temporary storage.")


def cleanup_loop():
    while True:
        time.sleep(300)
        cleanup_old_files()


threading.Thread(target=cleanup_loop, daemon=True).start()


@app.after_request
def security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Frame-Options"] = "DENY"
    return response


@app.get("/")
def home():
    return jsonify({
        "service": "Personal Clip Backend",
        "status": "online"
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/clip")
def create_clip():
    if not authorized():
        return jsonify({"error": "API Key गलत है या सेट नहीं है।"}), 401

    if rate_limited():
        return jsonify({
            "error": "बहुत ज्यादा अनुरोध। एक घंटे बाद फिर कोशिश करें।"
        }), 429

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify({"error": "सही JSON डेटा भेजें।"}), 400

    url = data.get("url", "")
    file_type = str(data.get("format", "mp4")).lower()
    quality = str(data.get("quality", "720"))

    if not isinstance(url, str) or not valid_youtube_url(url.strip()):
        return jsonify({"error": "सही HTTPS YouTube लिंक डालें।"}), 400

    url = url.strip()

    if file_type not in ALLOWED_FORMATS:
        return jsonify({"error": "यह फाइल फॉर्मेट उपलब्ध नहीं है।"}), 400

    if quality not in ALLOWED_QUALITIES:
        return jsonify({"error": "क्वालिटी का विकल्प सही नहीं है।"}), 400

    try:
        start = parse_time(data.get("start", "00:00:00"))
        end = parse_time(data.get("end", "00:01:00"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if end <= start:
        return jsonify({"error": "End Time, Start Time के बाद होना चाहिए।"}), 400

    if end - start > MAX_CLIP_SECONDS:
        return jsonify({"error": "एक क्लिप अधिकतम 10 मिनट की हो सकती है।"}), 400

    # एक समय में केवल एक प्रोसेसिंग जॉब।
    if not job_lock.acquire(blocking=False):
        return jsonify({"error": "सर्वर व्यस्त है। थोड़ी देर बाद कोशिश करें।"}), 429

    job_dir = None

    try:
        cleanup_old_files()

        job_id = uuid.uuid4().hex
        job_dir = CLIP_DIR / job_id
        job_dir.mkdir(mode=0o700)

        output_template = str(job_dir / "clip.%(ext)s")

        cmd = [
            "yt-dlp",
            "--ignore-config",
            "--no-playlist",
            "--no-warnings",
            "--no-progress",
            "--no-continue",
            "--no-part",
            "--socket-timeout", "15",
            "--retries", "1",
            "--fragment-retries", "1",
            "--max-filesize", "500M",
            "--download-sections", f"*{start}-{end}",
            "--output", output_template,
        ]

        if file_type == "mp3":
            cmd += [
                "-f", "bestaudio/best",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "5",
            ]

        elif file_type == "m4a":
            cmd += [
                "-f", "bestaudio[ext=m4a]/bestaudio",
                "-x", "--audio-format", "m4a",
            ]

        elif file_type == "webm":
            cmd += [
                "-f",
                f"bv*[height<={quality}][ext=webm]+ba[ext=webm]/"
                f"b[height<={quality}][ext=webm]",
                "--merge-output-format", "webm",
            ]

        else:
            cmd += [
                "-f",
                f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/"
                f"b[height<={quality}][ext=mp4]",
                "--merge-output-format", "mp4",
            ]

        cmd.append(url)

        # Shell=False (default): URL को shell command की तरह नहीं चलाया जाता।
        result = subprocess.run(
            cmd,
            cwd=str(job_dir),
            capture_output=True,
            text=True,
            timeout=MAX_PROCESS_SECONDS,
            check=False,
            shell=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "HOME": "/tmp",
                "TMPDIR": str(job_dir),
                "PYTHONUNBUFFERED": "1",
            },
        )

        if result.returncode != 0:
            # URL या पूरा stderr उपयोगकर्ता को नहीं दिखाते।
            app.logger.warning(
                "Video processing failed; exit code %s",
                result.returncode,
            )
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None

            return jsonify({
                "error": "वीडियो उपलब्ध नहीं है या सर्वर उसे प्रोसेस नहीं कर पाया।"
            }), 422

        candidates = [
            p for p in job_dir.iterdir()
            if p.is_file()
            and not p.is_symlink()
            and p.suffix.lower() in {".mp4", ".mp3", ".webm", ".m4a"}
        ]

        if not candidates:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({"error": "आउटपुट फाइल नहीं बनी।"}), 500

        output_file = max(candidates, key=lambda p: p.stat().st_size)

        return jsonify({
            "message": "क्लिप तैयार है।",
            "format": output_file.suffix.lower().lstrip("."),
            "job_id": job_id,
            "filename": output_file.name,
            "download_url": f"/download/{job_id}/{output_file.name}"
        })

    except subprocess.TimeoutExpired:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "समय सीमा पूरी हो गई। छोटी क्लिप आजमाएँ।"}), 504

    except Exception:
        app.logger.error("Processing failed.")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "सर्वर में समस्या आई।"}), 500

    finally:
        job_lock.release()


@app.get("/download/<job_id>/<filename>")
def download_file(job_id, filename):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "गलत फाइल आईडी।"}), 400

    if not re.fullmatch(r"clip\.(mp4|mp3|webm|m4a)", filename):
        return jsonify({"error": "गलत फाइल नाम।"}), 400

    job_dir = CLIP_DIR / job_id
    path = job_dir / filename

    if not path.is_file() or path.is_symlink():
        return jsonify({"error": "फाइल नहीं मिली या उसकी अवधि समाप्त हो गई।"}), 404

    if time.time() - path.stat().st_mtime >= FILE_TTL_SECONDS:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "फाइल की दो घंटे की अवधि समाप्त हो गई।"}), 410

    return send_from_directory(
        str(job_dir),
        filename,
        as_attachment=True,
        download_name=filename,
        conditional=True,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        debug=False,
    )
