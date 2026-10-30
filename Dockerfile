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
from urllib.request import Request, urlopenFROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg nodejs ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY app.py .

# app.py कॉपी होने के बाद yt-dlp का सबसे नया Nightly वर्ज़न इंस्टॉल करें (ताकि Docker कैश कभी पुराना वर्ज़न न रखे)
RUN pip install --no-cache-dir -U --pre "yt-dlp[default]"

RUN mkdir -p /tmp/clips \
    && chmod 700 /tmp/clips

EXPOSE 10000

CMD ["gunicorn", "--bind", "0.0.0.0:10000", "--workers", "1", "--threads", "4", "--timeout", "900", "app:app"]


from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

TURNSTILE_SECRET_KEY = os.environ.get("CLOUDFLARE_SECRET_KEY", "").strip()
YOUTUBE_COOKIES_TEXT = os.environ.get("YOUTUBE_COOKIES", "").strip()
SECRET_COOKIE_PATH = Path("/etc/secrets/cookies.txt")

ALLOWED_ORIGIN = os.environ.get(
    "ALLOWED_ORIGIN", "https://rkstudiohub.blogspot.com"
).strip().rstrip("/")

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

MAX_CLIP_SECONDS = 1800
MAX_PROCESS_SECONDS = 900
MAX_FILE_SIZE_BYTES = 500 * 1024 * 1024
FILE_TTL_SECONDS = 30 * 60
MAX_REQUESTS_PER_HOUR = 5

ALLOWED_FORMATS = {"mp4", "mp3", "webm", "m4a"}
ALLOWED_QUALITIES = {"best", "360", "480", "720", "1080"}

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
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()[:64]
    return (request.remote_addr or "unknown")[:64]


def parse_time(value):
    if isinstance(value, (int, float)):
        value = str(int(value))
    if not isinstance(value, str):
        raise ValueError("समय HH:MM:SS या MM:SS फॉर्मेट में लिखें।")

    clean_val = value.strip()
    if not clean_val:
        raise ValueError("कृपया समय खाली न छोड़ें।")

    parts = clean_val.split(":")
    if not 1 <= len(parts) <= 3:
        raise ValueError("समय का फॉर्मेट सही नहीं है (उदा. 00:02:00)।")

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
    if not isinstance(url, str) or len(url) > 200:
        return None

    url = url.strip()
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
        parts = [p for p in path.split("/") if p]
        if len(parts) == 1 and VIDEO_ID_RE.fullmatch(parts[0]):
            video_id = parts[0]
    else:
        if path == "/watch":
            qs = parse_qs(parsed.query)
            v_list = qs.get("v", [])
            if len(v_list) == 1 and VIDEO_ID_RE.fullmatch(v_list[0]):
                video_id = v_list[0]
        elif path.startswith("/shorts/") or path.startswith("/live/"):
            parts = [p for p in path.split("/") if p]
            if len(parts) == 2 and parts[0] in ("shorts", "live") and VIDEO_ID_RE.fullmatch(parts[1]):
                video_id = parts[1]

    if not video_id:
        return None

    return f"https://www.youtube.com/watch?v={video_id}"


def verify_turnstile(token):
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
                pass
    except OSError:
        pass


def cleanup_loop():
    while True:
        time.sleep(300)
        cleanup_old_files()


threading.Thread(target=cleanup_loop, daemon=True).start()


def write_valid_netscape_cookies(target_path):
    """Secret File या Environment Variable (चाहे एक लाइन में चिपक गया हो) से 100% मान्य कुकी फाइल बनाता है।"""
    raw_text = ""
    if SECRET_COOKIE_PATH.is_file():
        try:
            raw_text = SECRET_COOKIE_PATH.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            pass

    if not raw_text.strip():
        raw_text = YOUTUBE_COOKIES_TEXT

    if not raw_text.strip():
        return None

    # यदि Render के Env Var ने सभी लाइनों को एक ही लाइन में चिपका दिया है, तो उन्हें अलग करें
    normalized = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = re.sub(
        r"(?<!^)(?<!\n)\s+(?=(?:#HttpOnly_)?\.?(?:youtube\.com|google\.com)\s+(?:TRUE|FALSE)\s+)",
        "\n",
        normalized,
    )

    valid_lines = ["# Netscape HTTP Cookie File"]
    for raw_line in normalized.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") and not line.startswith("#HttpOnly_"):
            continue

        parts = line.split("\t")
        if len(parts) != 7:
            parts = line.split(None, 6)

        if len(parts) == 7:
            valid_lines.append("\t".join(parts))

    if len(valid_lines) <= 1:
        app.logger.warning("Cookies parsed 0 valid lines! Check YOUTUBE_COOKIES format.")
        return None

    target_path.write_text("\n".join(valid_lines) + "\n", encoding="utf-8")
    os.chmod(target_path, 0o600)
    app.logger.info("Successfully wrote %d cookie lines.", len(valid_lines) - 1)
    return target_path


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


def build_ytdlp_command(output_template, start, end, file_type, quality, clean_url, player_client, cookie_file=None):
    """
    Download whatever YouTube formats are available.
    - A selected resolution is preferred, but if it is unavailable, fall back
      to the best available stream instead of failing.
    - MP4 is recoded to MP4 when necessary; MP3/M4A are converted by FFmpeg.
    """
    cmd = [
        "yt-dlp",
        "--ignore-config",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-continue",
        "--no-part",
        "--geo-bypass",
        "--socket-timeout", "30",
        "--retries", "3",
        "--fragment-retries", "3",
        "--extractor-retries", "3",
        "--max-filesize", "500M",
        "--download-sections", f"*{start}-{end}",
        "--output", output_template,
    ]

    if player_client != "default":
        cmd += ["--extractor-args", f"youtube:player_client={player_client}"]

    if cookie_file and cookie_file.is_file():
        cmd += ["--cookies", str(cookie_file)]

    # Build a forgiving format selector. A requested resolution is tried first;
    # if that resolution/combination is missing, yt-dlp tries the best available.
    if quality == "best":
        video_selector = "bestvideo*+bestaudio/best"
    else:
        video_selector = (
            f"bv*[height<={quality}]+ba/"
            f"b[height<={quality}]/"
            "bestvideo*+bestaudio/best"
        )

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
        # Prefer WebM streams, but fall back to any available stream and convert.
        if quality == "best":
            webm_selector = (
                "bv*[ext=webm]+ba[ext=webm]/"
                "b[ext=webm]/bestvideo*+bestaudio/best"
            )
        else:
            webm_selector = (
                f"bv*[height<={quality}][ext=webm]+ba[ext=webm]/"
                f"b[height<={quality}][ext=webm]/"
                "bestvideo*+bestaudio/best"
            )
        cmd += [
            "-f", webm_selector,
            "--merge-output-format", "webm",
            "--recode-video", "webm",
        ]
    else:
        # MP4 output is created even when YouTube only provides another
        # container/codec, provided FFmpeg supports the required conversion.
        cmd += [
            "-f", video_selector,
            "--merge-output-format", "mp4",
            "--recode-video", "mp4",
        ]

    cmd.append(clean_url)
    return cmd


@app.post("/api/clip")
def create_clip():
    if rate_limited():
        return jsonify({
            "error": "आपकी प्रति घंटा 5 क्लिप की सीमा पूरी हो गई है। कृपया 1 घंटे बाद पुनः प्रयास करें।"
        }), 429

    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "अमान्य डेटा फॉर्मेट। कृपया पेज रिफ्रेश करके प्रयास करें।"}), 400

    token = data.get("cf_token", "")
    if not verify_turnstile(token):
        return jsonify({
            "error": "सुरक्षा जाँच (CAPTCHA) विफल रही। कृपया दोबारा टिक करके प्रयास करें।"
        }), 403

    raw_url = data.get("url", "")
    clean_url = normalize_and_validate_youtube_url(raw_url)
    if not clean_url:
        return jsonify({
            "error": "कृपया केवल मान्य YouTube Video, Shorts या Live का HTTPS लिंक ही डालें।"
        }), 400

    file_type = str(data.get("format", "mp4")).lower().strip()
    quality = str(data.get("quality", "best")).lower().replace("p", "").strip()

    if file_type not in ALLOWED_FORMATS:
        return jsonify({"error": "चयनित फाइल फॉर्मेट मान्य नहीं है।"}), 400

    if quality not in ALLOWED_QUALITIES:
        return jsonify({"error": "चयनित वीडियो क्वालिटी मान्य नहीं है।"}), 400

    try:
        start = parse_time(data.get("start", "00:00:00"))
        end = parse_time(data.get("end", "00:00:30"))
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    if end <= start:
        return jsonify({"error": "समाप्ति समय (End Time), शुरुआती समय (Start Time) से अधिक होना चाहिए।"}), 400

    if end - start > MAX_CLIP_SECONDS:
        return jsonify({"error": "एक बार में अधिकतम 30 मिनट (1800 सेकंड) तक की ही क्लिप काटी जा सकती है।"}), 400

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

        cookie_file = write_valid_netscape_cookies(job_dir / "cookies.txt")
        output_template = str(job_dir / "clip.%(ext)s")

        if cookie_file:
            client_strategies = [
                "default",
                "mweb,web",
                "tv_embedded,web_creator",
                "ios,android",
            ]
        else:
            client_strategies = [
                "ios,web_creator",
                "android,tv_embedded",
                "mweb,web",
            ]

        last_stderr = ""
        success = False

        for client_str in client_strategies:
            use_cookie = None if "ios" in client_str else cookie_file

            cmd = build_ytdlp_command(
                output_template, start, end, file_type, quality, clean_url, client_str, use_cookie
            )

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

            if result.returncode == 0:
                success = True
                break
            else:
                last_stderr = (result.stderr or "")[-500:]
                app.logger.warning("Strategy [%s] failed: %s", client_str, last_stderr)

        if cookie_file and cookie_file.exists():
            cookie_file.unlink(missing_ok=True)

        if not success:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None

            if "Sign in to confirm" in last_stderr or "bot" in last_stderr.lower():
                return jsonify({
                    "error": "YouTube बॉट-जाँच (Bot Check) सक्रिय है। कृपया Render के 'Secret Files' में cookies.txt जोड़ें।"
                }), 422

            return jsonify({
                "error": "इस वीडियो की स्ट्रीम निकालने में YouTube सर्वर ने मना किया। कृपया अलग क्वालिटी (जैसे 720p) या दूसरा लिंक आजमाएँ।"
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
            return jsonify({"error": "क्लिप तैयार नहीं हो सकी। कृपया समय की जाँच करें।"}), 500

        output_file = max(candidates, key=lambda p: p.stat().st_size)
        file_size = output_file.stat().st_size

        if file_size > MAX_FILE_SIZE_BYTES:
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir = None
            return jsonify({"error": "तैयार क्लिप 500 MB से बड़ी है। कृपया कम क्वालिटी चुनें।"}), 413

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
        return jsonify({"error": "प्रोसेसिंग का समय समाप्त हो गया। कृपया छोटी क्लिप या 720p क्वालिटी चुनें।"}), 504

    except Exception:
        app.logger.error("Unexpected error during processing.")
        if job_dir:
            shutil.rmtree(job_dir, ignore_errors=True)
        return jsonify({"error": "सर्वर में आंतरिक समस्या आई। कृपया पुनः प्रयास करें।"}), 500

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
