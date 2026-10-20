FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    DATA_DIR=/data

WORKDIR /app

COPY requirements.txt .
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install --no-cache-dir --require-hashes -r requirements.txt \
    && playwright install --with-deps chromium

COPY app.py monitor.py store.py ai.py public_network.py ./
COPY static ./static

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /data \
    && chown appuser:appuser /data
USER appuser

EXPOSE 8080
CMD ["python", "app.py"]
