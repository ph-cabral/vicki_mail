FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /app

# antiword: extracción de .doc viejo (formato binario, no soportado por python-docx/mammoth)
RUN apt-get update && apt-get install -y --no-install-recommends \
      tzdata curl antiword \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN useradd --create-home --uid 1000 appuser
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# 1 solo worker: el scheduler de polling corre en el proceso de la app;
# con más de 1 worker se duplicarían los jobs (mismo motivo que hikvsion_get_events).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
