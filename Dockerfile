FROM python:3.12-slim

# build-essential — fallback для случая если pip нужно собрать
# native extension у пакетов без wheel (rtms SDK имеет C код).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        git \
        build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ ./src/

# Нативный rtms SDK пишет диагностику в /app/logs/python_<pid>_*.log.
# Без этой папки — errno=2 спам и потеря критичных логов (media gateway
# reject 960 и т.п.). Создаём заранее, чтобы диагностика была всегда.
RUN mkdir -p /app/logs

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app

EXPOSE 8082

HEALTHCHECK --interval=60s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8082/health', timeout=3)" || exit 1

CMD ["python", "-m", "src.main"]
