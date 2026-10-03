# Triage service image. Built from the project root:
#   docker compose build triage
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY services/triage/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=appuser libs libs
COPY --chown=appuser services/__init__.py services/__init__.py
COPY --chown=appuser services/triage services/triage

USER appuser
EXPOSE 8001
CMD ["uvicorn", "services.triage.main:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"]
