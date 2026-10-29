
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /tmp/clips \
    && chmod 700 /tmp/clips

EXPOSE 10000

CMD ["gunicorn", "--workers", "1", "--threads", "2", "--timeout", "660", "--bind", "0.0.0.0:10000", "app:app"]
