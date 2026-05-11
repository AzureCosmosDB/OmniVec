FROM python:3.11-slim

WORKDIR /app

# System deps for PaddleOCR (libGL, libglib, etc.)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 libglib2.0-0 libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY worker.py .

# Pre-download PaddleOCR models during build to avoid download at runtime
RUN python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='en', show_log=False)"

EXPOSE 8080

CMD ["python", "worker.py"]
