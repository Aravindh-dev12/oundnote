FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    M2N_HOST=0.0.0.0 \
    M2N_PORT=10000 \
    M2N_OPEN_BROWSER=false \
    M2N_DATA_DIR=/var/data/oundnote \
    M2N_MODELS_DIR=/var/data/models

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl build-essential cmake pkg-config libopenblas-dev libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY adapters ./adapters
COPY api ./api

# CPU-first hosted configuration. Audio capture from the server's sound card is
# intentionally not installed; users upload audio or capture it in their browser.
ENV CMAKE_ARGS="-DGGML_NATIVE=OFF"
RUN pip install --no-cache-dir ".[transcription,diarization,summaries,rag,youtube]"

EXPOSE 10000
CMD ["sh", "-c", "M2N_PORT=${PORT:-10000} uvicorn local_meeting_ai.api.app:create_app --factory --host 0.0.0.0 --port ${PORT:-10000} --proxy-headers --forwarded-allow-ips='*'"]
