FROM python:3.12-slim

WORKDIR /app

LABEL org.opencontainers.image.title="AllDebrid-Client"
LABEL org.opencontainers.image.version="1.2.6"
LABEL org.opencontainers.image.description="Automated torrent downloading via AllDebrid with a branded web UI"

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl && rm -rf /var/lib/apt/lists/*

# Python deps
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir "asyncpg>=0.29.0"

# App
COPY backend/ /app/
COPY frontend/ /app/frontend/
COPY CHANGELOG.md /app/CHANGELOG.md
COPY VERSION /app/VERSION

# Directories
RUN mkdir -p /app/data/watch /app/data/processed /app/data/downloads /app/config

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -f http://localhost:8080/api/stats || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
