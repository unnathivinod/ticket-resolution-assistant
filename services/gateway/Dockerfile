# API gateway image. Built from the project root:
#   docker compose build gateway
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY services/gateway/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY --chown=appuser libs libs
COPY --chown=appuser services/__init__.py services/__init__.py
COPY --chown=appuser services/gateway services/gateway

USER appuser
EXPOSE 8000
CMD ["uvicorn", "services.gateway.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
