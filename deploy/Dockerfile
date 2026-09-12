FROM python:3.11-slim-bookworm
WORKDIR /app
COPY pyproject.toml /app/
COPY src /app/src
RUN pip install --no-cache-dir .
ENV DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "suwayomi_translator.app:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
