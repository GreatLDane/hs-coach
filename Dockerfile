FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8000 \
    HS_COACH_CARDS_JSON=/app/data/cards.json \
    HS_COACH_MODE=hosted \
    HS_COACH_TRUSTED_PROXIES=127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,fc00::/7

WORKDIR /app

COPY pyproject.toml README.md requirements.lock ./
COPY src ./src

RUN pip install --no-cache-dir -r requirements.lock && pip install --no-cache-dir --no-deps . && mkdir -p /app/data

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '8000') + '/api/health', timeout=3)"

CMD ["sh", "-c", "if [ ! -f /app/data/cards.json ]; then python -m hs_coach sync-cards --out /app/data/cards.json; fi && exec python -m hs_coach serve --host 0.0.0.0 --port ${PORT:-8000} --cards /app/data/cards.json --db /app/data/hs_coach.db"]
