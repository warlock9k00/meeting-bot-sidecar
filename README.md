# meeting-bot-sidecar

Python service на Hetzner VPS. Обрабатывает meeting recording'и которые CF Worker не вытягивает из-за 128 MB лимита памяти.

**Flow:**
```
Attendee webhook → CF Worker /webhook (HMAC validate)
                       ↓
                  CF KV "meeting_jobs" (bot_id + ts)
                       ↓ (polling каждые 15 сек)
                  sidecar (Python + ffmpeg):
                    - download mp4 из R2 (presigned)
                    - ffmpeg → opus 16k mono
                    - Groq Whisper Large v3
                    - render markdown source
                    - git commit → vault
                    - mark done в KV
```

**Зачем:** CF Worker падает на recordings >50 MB (~15 мин). Sidecar на Hetzner cx23 (4 GB RAM) обрабатывает любой размер.

## Setup

```bash
cp .env.example .env
# заполнить все секреты
docker compose up -d --build
docker compose logs -f sidecar
```

## Tests

```bash
pip install -r requirements.txt
PYTHONPATH=. pytest test/ -v
```
