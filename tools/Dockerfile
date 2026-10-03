# A small "toolbox" container for one-off jobs: generating data, loading it, running tests and evals.
# It means nobody needs Python installed on their own machine.
FROM python:3.11-slim

WORKDIR /app

COPY tools/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# The project folder is mounted at /app when the container runs (see docker-compose.yml).
CMD ["python", "--version"]
