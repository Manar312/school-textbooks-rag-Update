# One image, many roles (api / worker / beat / flower): the command decides the role.
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HF_HOME=/data/hf UV_LINK_MODE=copy

RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-ara tesseract-ocr-eng tesseract-ocr-fra curl \
    && rm -rf /var/lib/apt/lists/*

# uv = a much faster pip. Copied as a single static binary from its official image.
COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /srv
COPY requirements.txt .
# CPU-only torch FIRST (~200MB instead of ~2GB of CUDA libraries we cannot use on a plain VPS); the next
# step then sees torch already installed and skips it.
RUN uv pip install --system --index-url https://download.pytorch.org/whl/cpu torch \
 && uv pip install --system -r requirements.txt

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh && useradd -m rag && mkdir -p /data/storage /data/hf /tmp/prom && chown -R rag /data /srv /tmp/prom
USER rag

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
