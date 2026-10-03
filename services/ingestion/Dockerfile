# Ingestion worker image. Built from the project root:
#   docker compose build ingestion
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY services/ingestion/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=appuser libs libs
COPY --chown=appuser services/__init__.py services/__init__.py
COPY --chown=appuser services/ingestion services/ingestion

USER appuser
EXPOSE 8005
CMD ["python", "-m", "services.ingestion.worker"]
