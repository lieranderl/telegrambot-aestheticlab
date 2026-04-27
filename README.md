# Calendar Telegram Notifier

Small FastAPI service that watches one or more Google Calendars and forwards calendar changes to a Telegram chat. It stores Google Calendar sync tokens and webhook channel mappings in Google Secret Manager so the service can survive restarts without losing state.

## Features

- Watches multiple Google Calendars from a single service instance
- Sends event updates to a Telegram chat via the Bot API
- Persists per-calendar sync tokens in Google Secret Manager
- Persists Google webhook channel metadata for cleanup and re-registration
- Ships with a Dockerfile and GitHub Actions deployment workflow for Google Cloud Run

## Tech Stack

- Python 3.12
- FastAPI
- Uvicorn
- HTTPX
- Google Calendar API
- Google Cloud Secret Manager
- Docker
- GitHub Actions
- Google Cloud Run

## Repository Layout

```text
.
├── .github/workflows/deploy.yml   # Cloud Run deployment pipeline
├── Dockerfile                     # Container image build
├── pyproject.toml                 # Python package metadata and dependencies
├── tests/                         # Focused unit and service tests
├── src/
│   ├── app.py                     # App factory + startup wiring
│   ├── config.py                  # Environment-backed settings
│   ├── dependencies.py            # FastAPI dependency helpers
│   ├── main.py                    # Thin uvicorn entrypoint
│   ├── models.py                  # Typed domain models
│   ├── gateways/                  # Google/Telegram integrations
│   ├── routes/                    # HTTP endpoints
│   ├── services/                  # Formatting + orchestration
│   └── utils/                     # Small shared helpers
└── uv.lock                        # Locked dependency graph
```

## How It Works

The service exposes a public webhook endpoint that receives Google Calendar push notifications.

High-level flow:

1. `GET /register` creates a Google Calendar watch for every configured calendar.
2. Google sends webhook calls to `POST /webhook`.
3. The app looks up the webhook `channel_id` in Secret Manager to determine which calendar triggered the request.
4. If the calendar has no saved sync token yet, the app performs a seed sync and stores the first token without sending historical events.
5. For subsequent notifications, the app fetches delta changes with the saved sync token.
6. Each changed event is formatted into a Telegram message and sent to the configured chat.
7. The new sync token is stored back in Secret Manager.

State is stored in Google Secret Manager:

- `calendar-channel-map`
  Stores one line per active Google watch channel in the format `channel_id|resource_id|cal_id|label`
- `cal-sync-<sha1(calendar_id)>`
  Stores the latest Google Calendar sync token for one calendar

## Prerequisites

You need:

- Python 3.12
- `uv`
- A Telegram bot token
- A Telegram chat ID
- A Google Cloud project
- Application Default Credentials that can access:
  - Google Calendar API
  - Google Secret Manager
- The target calendars shared with the credentialed Google identity if you use a service account

## Configuration

The app reads configuration entirely from environment variables.

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_TOKEN` | Yes | Telegram bot token used for `sendMessage` |
| `TELEGRAM_CHAT_ID` | Yes | Telegram chat ID that receives notifications |
| `WEBHOOK_URL` | Yes | Public HTTPS URL Google Calendar should call |
| `CALENDAR_IDS` | Yes | Semicolon-separated list of `calendar_id|label` pairs |
| `GOOGLE_CLOUD_PROJECT` | Recommended | GCP project ID fallback when ADC does not provide one |
| `GCP_PROJECT` | Optional | Alternate project ID fallback |

Example:

```bash
export TELEGRAM_TOKEN="123456:telegram-bot-token"
export TELEGRAM_CHAT_ID="-1001234567890"
export WEBHOOK_URL="https://your-service.example.com/webhook"
export CALENDAR_IDS="primary|Main Calendar;team@example.com|Team Calendar"
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
```

`CALENDAR_IDS` format rules:

- Entries are separated by `;`
- Each entry must be `calendar_id|human label`
- Invalid entries are ignored with a warning

## Local Development

### 1. Install dependencies

```bash
uv sync
```

### 2. Authenticate to Google Cloud

The code uses Application Default Credentials.

For local development, one common approach is:

```bash
gcloud auth application-default login
```

If you use a service account locally, make sure the environment is configured for ADC and the calendars are shared with that account.

### 3. Export environment variables

```bash
export TELEGRAM_TOKEN="123456:telegram-bot-token"
export TELEGRAM_CHAT_ID="-1001234567890"
export WEBHOOK_URL="https://your-public-url/webhook"
export CALENDAR_IDS="primary|Main Calendar"
export GOOGLE_CLOUD_PROJECT="your-gcp-project"
```

### 4. Run the server

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080 --reload
```

Health check:

```bash
curl http://127.0.0.1:8080/health
```

Expected response:

```json
{"status":"ok"}
```

## Testing

Run the unit test suite:

```bash
uv run python -m unittest discover -s tests
```

Run line and branch coverage for `src/`:

```bash
uv sync
uv run coverage run -m unittest discover -s tests
uv run coverage report -m
```

The coverage configuration lives in [`pyproject.toml`](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/pyproject.toml) and currently enforces:

- source-only coverage for `src/`
- branch coverage enabled
- `fail_under = 100`

## Running with Docker

Build:

```bash
docker build -t calendar-telegram .
```

Run:

```bash
docker run --rm -p 8080:8080 \
  -e TELEGRAM_TOKEN="$TELEGRAM_TOKEN" \
  -e TELEGRAM_CHAT_ID="$TELEGRAM_CHAT_ID" \
  -e WEBHOOK_URL="$WEBHOOK_URL" \
  -e CALENDAR_IDS="$CALENDAR_IDS" \
  -e GOOGLE_CLOUD_PROJECT="$GOOGLE_CLOUD_PROJECT" \
  calendar-telegram
```

The image starts:

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8080
```

## HTTP Endpoints

### `GET /health`

Simple liveness endpoint.

### `POST /webhook`

Receives Google Calendar push notifications.

Required Google headers:

- `X-Goog-Channel-ID`
- `X-Goog-Resource-State`

Behavior:

- Returns `400` if required Google headers are missing
- Ignores `sync` handshake notifications
- Seeds an initial sync token if the calendar does not have one yet
- Fetches delta changes and sends one Telegram message per changed event

### `GET /register`

Creates a Google watch channel for every configured calendar and stores the mapping in Secret Manager.

Use this after:

- first deployment
- changing `CALENDAR_IDS`
- cleaning up channels
- recovering from lost channel state

### `GET /cleanup`

Stops all stored Google watch channels and resets the channel mapping secret.

### `GET /reset-secret`

Deletes and recreates the `calendar-channel-map` secret. This is a recovery endpoint for corrupted or destroyed channel mapping state.

### `GET /test-telegram`

Sends a hardcoded sample Telegram message. Useful for confirming the Telegram bot token and chat ID are correct.

## Google Calendar Watch Lifecycle

This service currently relies on manual watch registration.

Important operational detail:

- Google Calendar watch channels expire
- The current code does not renew channels automatically
- You will need to call `GET /register` again before or after expiration

If you run this in production, schedule a renewal workflow or add automatic renewal logic.

## Google Cloud Permissions

The runtime identity needs enough access to:

- read Google Calendar events
- create Google Calendar watches
- stop Google Calendar channels
- create, read, update, disable, and destroy Secret Manager secret versions

At minimum, review access for:

- Google Calendar API
- Secret Manager roles appropriate for read/write secret version management

## Deployment

The repository includes GitHub Actions deployment at [`.github/workflows/deploy.yml`](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/.github/workflows/deploy.yml).

Current deployment shape:

- builds and pushes a Docker image to Docker Hub
- authenticates to Google Cloud with Workload Identity Federation
- deploys the service to Cloud Run in `europe-west1`

The workflow sets:

- env vars:
  - `TELEGRAM_CHAT_ID`
  - `WEBHOOK_URL`
- Secret Manager-backed runtime secrets:
  - `CALENDAR_IDS`
  - `TELEGRAM_TOKEN`

Required GitHub secrets include:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`
- `DOCKER_IMAGE`
- `GCP_WORKLOAD_ID_PROVIDER`
- `GCP_SERVICE_ACCOUNT_GITHUB`
- `GCP_PROJECT`
- `GCP_SERVICE_ACCOUNT`
- `TELEGRAM_CHAT_ID`
- `WEBHOOK_URL`

## Production Checklist

- Verify the Cloud Run service account can access Calendar API and Secret Manager
- Share each calendar with the service account if required
- Confirm `WEBHOOK_URL` points to `/webhook`
- Call `GET /register` after deployment
- Confirm Google starts delivering notifications
- Confirm Telegram messages reach the target chat
- Put access control in front of non-webhook operational endpoints
- Add a process for watch renewal before expiration

## Operational Caveats

Current implementation characteristics worth knowing:

- The app now uses a small layered structure: routes, services, gateways, and settings
- Google clients are created during app startup instead of at module import
- The repository now includes focused tests for config parsing, formatting, mapping, and webhook orchestration
- The deployment workflow makes the service publicly reachable on Cloud Run
- Operational endpoints are state-changing and should be treated as admin-only

## Troubleshooting

### App fails at startup with missing env error

Make sure all required variables are set:

- `TELEGRAM_TOKEN`
- `TELEGRAM_CHAT_ID`
- `WEBHOOK_URL`
- `CALENDAR_IDS`

### `/register` says a calendar was not found or not shared

Common causes:

- wrong calendar ID
- calendar not shared with the service account or credentialed user
- Calendar API not enabled in the Google Cloud project

### No Telegram messages arrive

Check:

- bot token is valid
- bot has permission to post to the target chat
- chat ID is correct
- the service can reach `api.telegram.org`

### Webhook calls arrive but nothing is sent

Possible reasons:

- the webhook is a `sync` handshake
- the first change only seeded the initial sync token
- no deltas were returned for the stored sync token
- channel mapping was lost and `GET /register` needs to be run again

### Secret Manager issues mentioning destroyed versions

Use:

```bash
curl https://your-service.example.com/reset-secret
```

Then re-register channels:

```bash
curl https://your-service.example.com/register
```

## Suggested Next Improvements

- Move settings into a dedicated configuration model with validation
- Add authentication or signed verification for admin endpoints
- Renew Google watch channels automatically
- Save sync tokens only after successful message delivery
- Reuse a shared `httpx.AsyncClient`
- Add tests for message formatting, secret parsing, and webhook behavior

## License

MIT. See [LICENSE](/Users/evfedoto/Documents/Projects/telegrambot-aestheticlab/LICENSE).
