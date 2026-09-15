FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY teai_relay ./teai_relay

# Runs as an unprivileged user; Caddy terminates TLS in front of it.
# /data holds keys.json (keys added through the admin API); mounted as a volume.
RUN useradd --system --no-create-home relay && mkdir -p /data && chown relay:relay /data
VOLUME ["/data"]
USER relay

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "teai_relay"]
