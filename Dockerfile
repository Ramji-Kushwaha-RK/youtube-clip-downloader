FROM python:3.11-slim

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
