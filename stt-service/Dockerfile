FROM python:3.11-slim

# No GPU layer and no audio codec on purpose. This container is the security and
# buffering boundary; the model lives in stt-engine, reachable only over stt_net.

WORKDIR /srv/stt

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Holds no credentials of its own and no data; does not need root.
RUN useradd --system --create-home --uid 10002 stt
USER stt

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
