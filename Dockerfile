FROM python:3.12-slim

WORKDIR /app

# Install system dependencies (e.g., curl for healthchecks)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && (python -m spacy download pl_core_news_lg || python -m spacy download pl_core_news_sm)

COPY . .

# Default command can be overridden in docker-compose.yml
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
