# Prediction-market arbitrage bot — paper mode by default.
# Live trading requires credentials via env and an explicit --mode live.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
# Freeze the exact resolved versions into the image for reproducibility
# audits: docker compose run --rm arb-bot cat /requirements.lock.txt
RUN pip install -r requirements.txt && pip freeze > /requirements.lock.txt

COPY arb_bot.py ticker_return.py ./

# Non-root user; data/ is the writable volume (state.db, CSVs, KILL file)
RUN useradd --create-home bot \
    && mkdir -p /app/data /app/keys \
    && chown -R bot:bot /app
USER bot

# Keyed off the /health endpoint (HEALTH_PORT=8080 set in compose).
# start-period covers WS connect + initial snapshots.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]

# Exec form: the bot's own SIGTERM handler runs graceful shutdown
# (drain in-flight legs -> close clients -> release run lock).
CMD ["python", "arb_bot.py"]
