FROM python:3.11-slim

WORKDIR /app

# Install system deps needed by psycopg2-binary
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
RUN pip install --no-cache-dir -e .

COPY main.py .
COPY src/ src/

ENTRYPOINT ["python", "main.py"]
