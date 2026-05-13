# OmniVec Agent — Phase 1 (read-only diagnostics)
# Multi-stage build: build deps in stage 1, slim runtime in stage 2.
FROM python:3.12-slim AS builder
WORKDIR /build
COPY agent/requirements.txt /build/requirements.txt
RUN pip install --no-cache-dir --prefix=/install -r /build/requirements.txt

FROM python:3.12-slim
RUN groupadd -r app && useradd -r -g app -u 10001 app
WORKDIR /app
COPY --from=builder /install /usr/local
COPY agent /app/agent
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8000/v1/health',timeout=3); sys.exit(0)" || exit 1
CMD ["uvicorn", "agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
