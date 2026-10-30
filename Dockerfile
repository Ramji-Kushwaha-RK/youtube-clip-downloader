FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

# FFmpeg is used for clipping/conversion. Node.js 22 is required by the
# BgUtils PO Token provider server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg ca-certificates curl gnupg git \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir -U --pre "yt-dlp[default]"

# Build the matching BgUtils PO Token HTTP provider locally.
RUN git clone --depth 1 --single-branch --branch 2.0.2 \
      https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git \
      /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && npm ci \
    && npx tsc

COPY app.py .

RUN mkdir -p /tmp/clips \
    && chmod 700 /tmp/clips

EXPOSE 10000

# Keep the token provider private to this container; only Gunicorn is public.
CMD ["sh", "-c", "node /opt/bgutil-ytdlp-pot-provider/server/build/main.js --host 127.0.0.1 --port 4416 & exec gunicorn --bind 0.0.0.0:10000 --workers 1 --threads 4 --timeout 900 app:app"]
