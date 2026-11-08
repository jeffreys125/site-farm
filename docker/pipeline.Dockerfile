# =============================================================================
#  Content pipeline image: Python + the sitefarm package.
#  Runs either as a one-shot command or as a daily scheduled loop.
# =============================================================================
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SITEFARM_CONFIG=/app/runtime/sites.yaml

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY run.py ./
COPY sitefarm/ ./sitefarm/
COPY docker/pipeline-entrypoint.sh /usr/local/bin/pipeline-entrypoint.sh
RUN chmod +x /usr/local/bin/pipeline-entrypoint.sh \
    && mkdir -p /app/data /app/output /app/runtime

VOLUME ["/app/data", "/app/output"]

ENTRYPOINT ["/usr/local/bin/pipeline-entrypoint.sh"]
