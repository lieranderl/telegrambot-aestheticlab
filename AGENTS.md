# Agent Instructions

## Package Manager
- Use **uv** only: `uv sync`, `uv run ...`
- Python version is pinned to `==3.12.*` in `pyproject.toml`
- Keep `uv.lock` in sync when dependencies change

## Project Shape
- FastAPI app factory: `src/app.py`
- Uvicorn entrypoint: `src/main.py`
- Environment settings: `src/config.py`
- HTTP routes: `src/routes/`
- External integrations: `src/gateways/`
- Business orchestration: `src/services/`
- Domain models and shared helpers: `src/models.py`, `src/utils/`

## Local Commands
| Task | Command |
|------|---------|
| Install | `uv sync` |
| Run server | `uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload` |
| Full tests | `uv run python -m unittest discover -s tests` |
| Coverage | `uv run coverage run -m unittest discover -s tests && uv run coverage report -m` |
| Docker build | `docker build -t calendar-telegram .` |

## File-Scoped Commands
| Task | Command |
|------|---------|
| Single test file | `uv run python -m unittest tests/test_config.py` |
| Single test case | `uv run python -m unittest tests.test_config.SettingsTests.test_from_env_raises_for_missing_values` |

## Testing
- Maintain `100%` line and branch coverage enforced in `pyproject.toml`
- Prefer focused `unittest` tests under `tests/` with mocks/fakes for Google, Telegram, HTTP, and Secret Manager calls
- Do not require live Google Cloud, Telegram, or network access for unit tests

## Runtime Contracts
- Config comes only from environment variables; keep names aligned with `README.md`
- Required env: `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `WEBHOOK_URL`, `CALENDAR_IDS`
- Keep secrets out of source, logs, tests, and docs examples
- Preserve webhook behavior for Google Calendar headers and `sync` handshake notifications
- Secret Manager state formats are part of the service contract:
  - `calendar-channel-map`: `channel_id|resource_id|cal_id|label`
  - `cal-sync-<sha1(calendar_id)>`: latest sync token

## Code Conventions
- Follow existing small-module layout; keep route handlers thin and put orchestration in services
- Keep gateways as integration boundaries; do not call Google, Telegram, or Secret Manager clients directly from routes
- Use typed dataclasses/models for shared data instead of unstructured dicts where practical
- Prefer dependency injection through `AppServices` and app state for testability
- Avoid adding new frameworks, background schedulers, or deployment tooling unless requested

## Deployment
- Cloud Run deployment is defined in `.github/workflows/deploy.yml`
- Container startup is defined in `Dockerfile`
- Public endpoints are intentionally exposed by Cloud Run; treat admin routes as operationally sensitive

## Commit Attribution
AI commits MUST include:
```
Co-Authored-By: (the agent model's name and attribution byline)
```
