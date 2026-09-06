FROM python:3.11-slim@sha256:9534e5a8e315485d4061ed659af0fd78a284c015f9b73661b41d6bab25604534

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ORPHEUS_RUNTIME_MODE=cloud_run

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-cloud.txt ./
RUN pip install --no-cache-dir -r requirements-cloud.txt
COPY orpheus ./orpheus
COPY frontend/dist ./frontend/dist

RUN useradd --create-home --uid 10001 orpheus \
    && mkdir -p /var/lib/orpheus \
    && chown -R orpheus:orpheus /app /var/lib/orpheus
USER orpheus
ENV ORPHEUS_DATA_DIR=/var/lib/orpheus

EXPOSE 8080
CMD ["python", "-m", "orpheus.server.web"]
