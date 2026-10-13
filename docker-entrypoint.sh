#!/bin/sh
set -e
mkdir -p "$TESLA_FLEET_HOME" "$FASTMCP_HOME"
chown -R app:app /data
chmod 700 "$TESLA_FLEET_HOME"
exec setpriv --reuid=app --regid=app --init-groups "$@"
