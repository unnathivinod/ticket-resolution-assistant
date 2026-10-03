# Retrieval service image. Built from the project root:
#   docker compose build retrieval
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY services/retrieval/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=appuser libs libs
COPY --chown=appuser services/__init__.py services/__init__.py
COPY --chown=appuser services/ingestion services/ingestion
COPY --chown=appuser services/retrieval services/retrieval

USER appuser
EXPOSE 8002
CMD ["uvicorn", "services.retrieval.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
