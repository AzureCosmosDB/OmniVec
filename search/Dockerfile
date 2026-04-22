FROM python:3.11-slim

WORKDIR /app

RUN groupadd -r omnivec && useradd -r -g omnivec -u 1000 omnivec

COPY search/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY search/ ./

USER omnivec

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
