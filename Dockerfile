FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TESLA_FLEET_HOME=/data/tesla \
    FASTMCP_HOME=/data/fastmcp \
    PORT=8000

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tesla_fleet ./tesla_fleet
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 app

# Tokens, ledger, audit log and OAuth state live on the mounted volume at /data.
# The entrypoint fixes volume ownership, then drops root before starting the server.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "tesla_fleet.remote"]
