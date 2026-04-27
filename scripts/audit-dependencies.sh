#!/usr/bin/env bash
set -euo pipefail

cache_dir="${UV_CACHE_DIR:-.tmp/uv-cache}"
requirements_file="${AUDIT_REQUIREMENTS_FILE:-.tmp/requirements-audit.txt}"

mkdir -p "$(dirname "${requirements_file}")" "${cache_dir}"

uv export \
  --frozen \
  --cache-dir "${cache_dir}" \
  --format requirements-txt \
  --no-dev \
  --no-emit-project \
  --no-hashes \
  --output-file "${requirements_file}" >/dev/null

uv run --cache-dir "${cache_dir}" pip-audit \
  --strict \
  --disable-pip \
  --no-deps \
  -r "${requirements_file}"
