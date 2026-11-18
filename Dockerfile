FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

RUN useradd --system --uid 10001 --create-home oloop \
    && mkdir -p /data/blobs && chown -R oloop /data

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir ".[postgres]"

USER oloop
EXPOSE 8000

# Plain HTTP on purpose: a reverse proxy in front ends TLS.
CMD ["oloop-server", "serve", "--host", "0.0.0.0", "--port", "8000"]
