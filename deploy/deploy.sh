#!/bin/sh
# Run on the server (by GitHub Actions or by hand). Never uses `down -v`: volumes (data) are kept.
set -e
git pull --ff-only
docker compose build
docker compose up -d --remove-orphans
docker image prune -f
docker compose ps
