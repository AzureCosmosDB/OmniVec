FROM python:3.11-slim

WORKDIR /app
COPY connectors/ingestion/onelake_iceberg/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY connectors/ingestion/onelake_iceberg/ .
COPY omnivec_queue/ omnivec_queue/

CMD ["python", "main.py"]
