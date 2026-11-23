# ClipSafe AI backend: FastAPI + FFmpeg + yt-dlp + demucs, built for Cloud Run (gen2).

# Deno is the JavaScript runtime yt-dlp uses to solve YouTube player challenges.
FROM denoland/deno:bin-2.9.4 AS deno

FROM python:3.11-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/models/hf \
    TORCH_HOME=/opt/models/torch \
    DEMUCS_MODEL=htdemucs \
    WORK_DIR=/tmp/clipsafe \
    FONT_DIR=/usr/share/fonts \
    PORT=8080

# ffmpeg (with libass + freetype for animated captions), fonts for subtitles,
# libsndfile for audio I/O, tini as PID 1 so FFmpeg child processes are reaped.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      fontconfig \
      fonts-noto-core \
      fonts-noto-color-emoji \
      fonts-dejavu-core \
      fonts-liberation \
      libsndfile1 \
      ca-certificates \
      curl \
      tini \
 && fc-cache -f \
 && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno

RUN useradd --create-home --uid 10001 app

WORKDIR /app

COPY requirements.txt .
RUN pip install --upgrade pip setuptools wheel \
 && pip install -r requirements.txt

# Bake the demucs weights into the image so cold starts never download ~80 MB.
RUN mkdir -p /opt/models \
 && python -c "from demucs.pretrained import get_model; get_model('${DEMUCS_MODEL}')" \
 && chmod -R a+rX /opt/models

# Sanity checks: fail the build early if a required binary is missing.
RUN ffmpeg -hide_banner -filters | grep -qE '^ ... (ass|subtitles) ' \
 && ffmpeg -hide_banner -filters | grep -q ' drawtext ' \
 && deno --version \
 && yt-dlp --version

COPY --chown=app:app . .

RUN mkdir -p "${WORK_DIR}" && chown app:app "${WORK_DIR}"

USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/healthz" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec uvicorn main:app --host 0.0.0.0 --port ${PORT} --workers 1 --timeout-keep-alive 75 --proxy-headers --forwarded-allow-ips='*'"]
