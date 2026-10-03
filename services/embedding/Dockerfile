# Embedding service image. Built from the project root:
#   docker compose build embedding
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EMBEDDING_CACHE_DIR=/models

WORKDIR /app

# Run as a normal user, not root (a basic container security practice).
RUN useradd --create-home --uid 10001 appuser \
    && mkdir /models \
    && chown appuser /models

COPY services/embedding/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Which meaning model to bake into the image. Change it in .env (EMBEDDING_DENSE_MODEL) and rebuild.
ARG EMBEDDING_DENSE_MODEL=BAAI/bge-small-en-v1.5
ENV EMBEDDING_DENSE_MODEL=${EMBEDDING_DENSE_MODEL}

# Download the models now, in their own layer. Docker reuses this layer on later
# builds, so changing main.py does not download the models again.
COPY services/__init__.py services/__init__.py
COPY services/embedding/__init__.py services/embedding/config.py services/embedding/models.py \
     services/embedding/download_models.py services/embedding/
USER appuser
RUN python -m services.embedding.download_models

COPY --chown=appuser libs libs
COPY --chown=appuser services/embedding services/embedding

EXPOSE 8004
CMD ["uvicorn", "services.embedding.main:app", "--host", "0.0.0.0", "--port", "8004", "--no-access-log"]
