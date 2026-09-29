# One image, two services (API + dashboard) - see docker-compose.yml
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 MLFLOW_DISABLE_AGENT_HINT=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 curl tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# train at build time so the image is self-contained and reproducible
RUN python -m src.pipeline
RUN useradd -m appuser && chown -R appuser /app
USER appuser
EXPOSE 8000 8501
HEALTHCHECK CMD curl -fs http://localhost:8000/health || curl -fs http://localhost:8501/_stcore/health || exit 1
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
