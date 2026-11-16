FROM python:3.12.16-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG UV_VERSION=0.11.3

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:${PATH}"

RUN pip install --no-cache-dir "uv==${UV_VERSION}" \
    && groupadd --system app \
    && useradd --system --gid app --home-dir /app app

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY src ./src

RUN uv sync --frozen --no-dev --no-install-project \
    && chown -R app:app /app /opt/venv

USER app

CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8080"]
