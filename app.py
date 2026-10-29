import os
import re
import json
import time
import uuid
import shutil
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode
from urllib.request import Request, urlopen

from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024  # अधिकतम 16 KB JSON पेलोड

# Environment Variables (Render में सेट करने के लिए)
TURNSTILE_SECRET_KEY = os.environ.get("CLOUDFLARE_SECRET_KEY", "").strip()
ALLOWED_ORIGIN = os.environ.get(
    "ALLOWED_ORIGIN", "https://rkstudiohub.blogspot.com"
).strip().rstrip("/")

# CORS सेटअप: आपके Blogger पेज को अनुमति देने के लिए
CORS(
    app,
    resources={
        r"/api/*": {
            "origins": [ALLOWED_ORIGIN] if ALLOWED_ORIGIN else ["*"],
            "allow_headers": ["Content-Type"],
            "methods": ["POST", "OPTIONS"],
        }
    },
)

CLIP_DIR = Path("/tmp/clips")
CLIP_DIR.mkdir(parents=True, exist_ok=True)
os.chmod(CLIP_DIR, 0o700)

MAX_CLIP_SECONDS = 600          # अधिकतम 10 मिनट की क्लिप
MAX_PROCESS_SECONDS = 480       # प्रोसेसिंग टाइमआउट (8 मिनट)
MAX_FILE_SIZE_BYTES = 300 * 1024 * 1024  # 300 MB सीमा
FILE_TTL_SECONDS = 30 * 60      # 30 मिनट में फाइल अपने-आप डिलीट होगी
MAX_REQUESTS_PER_HOUR = 10      # प्रति घंटा प्रति IP अधिकतम 10 अनुरोध

ALLOWED_FORMATS = {"mp4", "mp3", "webm", "m4a"}
ALLOWED_QUALITIES = {"360", "480", "720", "1080"}

ALLOWED_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

request_times = {}
rate_lock = threading.Lock()
job_lock = threading.Lock()


def get_client_ip():
    """Render के प्रॉक्सी के पीछे से असली यूजर IP सुरक्षित रूप से निकालें।"""
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def parse_time(value):
    """HH:MM:SS, MM:SS या सेकंड को सुरक्षित रूप से पूर्णांक (seconds) में बदलें।"""
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        raise ValueError("समय HH:MM:SS या MM:SS फॉर्मेट में लिखें।")

    clean_val = value.strip()
    if not clean_val:
        raise ValueError("कृपया समय खाली न छोड़ें।")

    parts = clean_val.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("समय का फॉर्मेट सही नहीं है (उदा. 00:01:30)।")

    if not all(part.isdigit() for part in parts):
        raise ValueError("समय में केवल अंक (0-9) और कोलन (:) का प्रयोग करें।")

    nums = [int(part) for part in parts]
    if any(n < 0 for n in nums):
        raise ValueError("समय ऋणात्मक नहीं हो सकता।")

    if len(nums) == 3:
        if nums[1] >= 60 or nums[2] >= 60:
            raise ValueError("मिनट और सेकंड 59 से अधिक नहीं हो सकते।")
        return nums[0] * 3600 + nums[1] * 60 + nums[2]

    if len(nums) == 2:
        if nums[1] >= 60:
            raise ValueError("सेकंड 59 से अधिक नहीं हो सकते।")
        return nums[0] * 60 + nums[1]

    return nums[0]


def normalize_and_validate_youtube_url(url):
    """
    केवल असली YouTube Video, Shorts और Live URLs को अनुमति दें।
    किसी भी स्क्रिप्ट, प्लेलिस्ट या अवांछित पैरामीटर को हटाकर साफ URL लौटाएँ।
    """
    if not isinstance(url, str) or len(url) > 200:
        return None

    url = url.strip()
    # किसी भी खतरनाक कैरेक्टर या कमांड फ्लैग को तुरंत ब्लॉक करें
    if url.startswith("-") or any(c in url for c in ["<", ">", '"', "'", ";", "|", "&", "`", "$", "\n", "\r"]):
        # ध्यान दें: watch?v=...&feature=... हो सकता है, इसलिए पहले urlparse करेंगे
        pass

    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    if parsed.scheme != "https" or not parsed.hostname:
        return None

    host = parsed.hostname.lower()
    if host not in ALLOWED_HOSTS:
        return None

    if parsed.username is not None or parsed.password is not None or parsed.port not in (None, 443):
        return None

    video_id = None
    path = parsed.path or ""

    if host == "youtu.be":
        # Format: https://youtu.be/VIDEO_ID
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1 and VIDEO_ID_RE.fullmatch(parts[0]):
            video_id = parts[0]
    else:
        # Format 1: /watch?v=VIDEO_ID
        if path == "/watch":
            qs = parse_qs(parsed.query)
            v_list = qs.get("v", [])
            if len(v_list) == 1 and VIDEO_ID_RE.fullmatch(v_list[0]):
                video_id = v_list[0]
        # Format 2: /shorts/VIDEO_ID या /live/VIDEO_ID
        elif path.startswith("/shorts/") or path.startswith("/live/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] in ("shorts", "live") and VIDEO_ID_RE.fullmatch(parts[1]):
                video_id = parts[1]

    if not video_id:
        return None

    # हमेशा साफ और सुरक्षित मानक URL लौटाएँ ताकि कोई अतिरिक्त पैरामीटर yt-dlp तक न पहुँचे
    return f"https://www.youtube.com/watch?v={video_id}"


def verify_turnstile(token):
    """Cloudflare Turnstile CAPTCHA टोकन की सर्वर-साइड जाँच करें।"""
    if not TURNSTILE_SECRET_KEY:
        app.logger.error("CLOUDFLARE_SECRET_KEY सेट नहीं है।")
        return False

    if not isinstance(token, str) or not (10 <= len(token) <= 2048):
        return False

    try:
        payload = urlencode({
            "secret": TURNSTILE_SECRET_KEY,
            "response": token,
            "remoteip": get_client_ip(),
        }).encode("utf-8")

        req = Request(
            "https://challenges.cloudflare.com/turnstile/v0/siteverify",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urlopen(req, timeout=8) as resp:
            result = json.loads(resp.read().decode("utf-8"))
            return bool(result.get("success") is True)
    except Exception as exc:
        app.logger.warning("Turnstile verification error: %s", exc)
        return False


def rate_limited():
    client = get_client_ip()
    now = time.time()

    with rate_lock:
        recent = [stamp for stamp in request_times.get(client, []) if now - stamp < 3600]
        if len(recent) >= MAX_REQUESTS_PER_HOUR:
            request_times[client] = recent
            return True

        recent.append(now)
        request_times[client] = recent

        for ip in list(request_times):
            request_times[ip] = [s for s in request_times[ip] if now - s < 3600]
            if not request_times[ip]:
                del request_times[ip]

    return False


def cleanup_old_files():
    now = time.time()
    try:
        for path in CLIP_DIR.iterdir():
            try:
                if path.is_dir() and not path.is_symlink():
                    files = list(path.rglob("*"))
                    timestamps = [
                        item.stat().st_mtime
                        for item in files
                        if item.is_file() and not item.is_symlink()
                    ]
                    last_modified = max(timestamps + [path.stat().st_mtime])
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
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response


@app.get("/")
def home():
    return jsonify({
        "service": "RK Studio Hub Clip Backend",
        "status": "online"
    })


@app.get("/health")
def health():
    return jsonify({"status": "ok"})


@app.post("/api/clip")
def create_clip():
    if rate_limited():
        return jsonify({
            "error": "आपने बहुत अधिक अनुरोध किए हैं। कृपया 1 घंटे बाद पुनः प्रयास करें।"
        }), 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "अमान्य डेटा फॉर्मेट। कृपया पेज रिफ्रेश करके प्रयास करें।"}), 400

    # 1. Cloudflare Turnstile CAPTCHA जाँच
    token = data.get("cf_token", "")
    if not verify_turnstile(token):
        return jsonify({
            "error": "सुरक्षा जाँच (CAPTCHA) विफल रही या समाप्त हो गई। कृपया दोबारा टिक करके प्रयास करें।"
        }), 403

    # 2. YouTube URL जाँच (केवल Video, Shorts, Live)
    raw_url = data.get("url", "")
    clean_url = normalize_and_validate_youtube_url(raw_url)
    if not clean_url:
        return jsonify({
            "error": "कृपया केवल मान्य YouTube Video, Shorts या Live का HTTPS लिंक ही डालें।"
        }), 400

    # 3. फॉर्मेट और क्वालिटी जाँच
    file_type = str(data.get("format", "mp4")).lower().strip()
    quality = str(data.get("quality", "720")).lower().replace("p", "").strip()

    if file_type not in ALLOWED_FORMATS:
        return jsonify({"error": "चयनित फाइल फॉर्मेट मान्य नहीं है (केवल MP4, MP3, M4A, WebM)।"}), 400

    if quality not in ALLOWED_QUALITIES:
        return jsonify({"error": "चयनित वीडियो क्वालिटी मान्य नहीं है (केवल 360p, 480p, 720p, 1080p)।"}), 400

    # 4. समय सीमा जाँच
    try:
        start = parse_time(data.get("start", "00:00:00"))
        end = parse_time(data.get("end", "00:00:30"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if end <= start:
        return jsonify({"error": "समाप्ति समय (End Time), शुरुआती समय (Start Time) से अधिक होना चाहिए।"}), 400

    if end - start > MAX_CLIP_SECONDS:
        return jsonify({"error": "एक बार में अधिकतम 10 मिनट (600 सेकंड) तक की ही क्लिप काटी जा सकती है।"}), 400

    # 5. सर्वर लोड सुरक्षा: एक समय में एक प्रोसेसिंग जॉब
    if not job_lock.acquire(blocking=False):
        return jsonify({
            "error": "सर्वर अभी एक अन्य क्लिप तैयार कर रहा है। कृपया 30-40 सेकंड बाद पुनः प्रयास करें।"
        }), 429

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
            "--max-filesize", "300M",
            "--download-sections", f"*{start}-{end}",
            "--force-keyframes-at-cuts",
            "--output", output_template,
        ]

        if file_type == "mp3":
            cmd += [
                "-f", "bestaudio/best",
                "-x", "--audio-format", "mp3",
                "--audio-quality", "192K",
            ]
        elif file_type == "m4a":
            cmd += [
                "-f", "bestaudio[ext=m4a]/bestaudio/best",
                "-x", "--audio-format", "m4a",
            ]
        elif file_type == "webm":
            cmd += [
                "-f",
                f"bv*[height<={quality}][ext=webm]+ba[ext=webm]/b[height<={quality}][ext=webm]/bv*[height<={quality}]+ba/b[height<={quality}]",
                "--merge-output-format", "webm",
            ]
        else:
            cmd += [
                "-f",
                f"bv*[height<={quality}][ext=mp4]+ba[ext=m4a]/b[height<={quality}][ext=mp4]/bv*[height<={quality}]+ba/b[height<={quality}]",
                "--merge-output-format", "mp4",
            ]

        cmd.append(clean_url)

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
            app.logger.warning("yt-dlp failed with code %s", result.returncode)
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({
                "error": "यह वीडियो डाउनलोड के लिए उपलब्ध नहीं है या YouTube द्वारा प्रतिबंधित है।"
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
            return jsonify({"error": "क्लिप तैयार नहीं हो सकी। कृपया छोटा समय चुनें।"}), 500

        output_file = max(candidates, key=lambda p: p.stat().st_size)
        file_size = output_file.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({"error": "तैयार क्लिप 300 MB की अधिकतम सीमा से बड़ी है। कृपया कम क्वालिटी या छोटा समय चुनें।"}), 413

        return jsonify({
            "message": "आपकी क्लिप सफलतापूर्वक तैयार हो गई है!",
            "format": output_file.suffix.lower().lstrip("."),
            "size_mb": round(file_size / (1024 * 1024), 2),
            "job_id": job_id,
            "filename": output_file.name,
            "download_url": f"/download/{job_id}/{output_file.name}"
        })

    except subprocess.TimeoutExpired:
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "प्रोसेसिंग की समय-सीमा समाप्त हो गई। कृपया छोटी क्लिप या कम क्वालिटी आजमाएँ।"}), 504

    except Exception:
        app.logger.error("Unexpected error during processing.")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "सर्वर में आंतरिक समस्या आई। कृपया कुछ देर बाद प्रयास करें।"}), 500

    finally:
        job_lock.release()


@app.get("/download/<job_id>/<filename>")
def download_file(job_id, filename):
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return jsonify({"error": "अमान्य डाउनलोड लिंक।"}), 400

    if not re.fullmatch(r"clip\.(mp4|mp3|webm|m4a)", filename):
        return jsonify({"error": "अमान्य फाइल नाम।"}), 400

    job_dir = CLIP_DIR / job_id
    path = job_dir / filename

    if not path.is_file() or path.is_symlink():
        return jsonify({"error": "फाइल नहीं मिली या समय समाप्त होने के कारण हटा दी गई है।"}), 404

    if time.time() - path.stat().st_mtime >= FILE_TTL_SECONDS:
        shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "डाउनलोड लिंक की 30 मिनट की वैधता समाप्त हो गई है।"}), 410

    return send_from_directory(
        str(job_dir),
        filename,
        as_attachment=True,
        download_name=f"rkstudio_{job_id[:6]}_{filename}",
        conditional=True,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "10000")),
        debug=False,
    )
