import os
import uuid
import logging
import json
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from googleapiclient.discovery import build
from google.cloud import secretmanager_v1
from google.api_core.exceptions import NotFound, AlreadyExists
from google.auth import default as google_auth_default

# --- Logging ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_URL]):
    raise RuntimeError("❌ Missing one or more required environment variables")

# Parse calendars: id1|Label1;id2|Label2
CALENDARS: dict[str, str] = {}
for pair in os.getenv("CALENDAR_IDS", "").split(";"):
    if "|" in pair:
        cal_id, label = pair.split("|", 1)
        CALENDARS[cal_id.strip()] = label.strip()

if not CALENDARS:
    raise RuntimeError("❌ No valid calendars configured in CALENDAR_IDS")

logger.info(f"📅 Configured calendars: {CALENDARS}")

# --- Google clients ---
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
credentials, project_id = google_auth_default(scopes=SCOPES)
logger.info(f"🔑 Using ADC. Effective project: {project_id}")

calendar_service = build(
    "calendar", "v3", credentials=credentials, cache_discovery=False
)
secret_client = secretmanager_v1.SecretManagerServiceClient(credentials=credentials)


# --- Helpers ---
async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text}
            )
            resp.raise_for_status()
        logger.info("✅ Sent message to Telegram")
    except Exception as e:
        logger.error(f"❌ Failed to send Telegram message: {e}")


def secret_name_for(key: str) -> str:
    return f"projects/{project_id}/secrets/{key}"


def get_secret(key: str) -> Optional[str]:
    try:
        response = secret_client.access_secret_version(
            name=f"{secret_name_for(key)}/versions/latest"
        )
        return response.payload.data.decode("utf-8")
    except NotFound:
        return None
    except Exception as e:
        logger.error(f"⚠️ Error fetching secret {key}: {e}")
        return None


def save_secret(key: str, value: str):
    parent = f"projects/{project_id}"
    try:
        secret_client.create_secret(
            parent=parent,
            secret_id=key,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                )
            ),
        )
        logger.info(f"🔐 Created new secret {key}")
    except AlreadyExists:
        pass
    except Exception as e:
        logger.error(f"❌ Failed to create secret {key}: {e}")
        return

    try:
        secret_client.add_secret_version(
            parent=f"{parent}/secrets/{key}",
            payload=secretmanager_v1.SecretPayload(data=value.encode("utf-8")),
        )
        logger.info(f"💾 Updated secret {key}")
    except Exception as e:
        logger.error(f"❌ Failed to save secret {key}: {e}")


# --- Event Processing ---
async def process_calendar(cal_id: str, label: str):
    sync_token = get_secret(f"sync-token-{cal_id}")
    try:
        if sync_token:
            events = (
                calendar_service.events()
                .list(
                    calendarId=cal_id,
                    syncToken=sync_token,
                    singleEvents=True,
                )
                .execute()
            )
        else:
            events = (
                calendar_service.events()
                .list(
                    calendarId=cal_id,
                    maxResults=5,
                    orderBy="updated",
                    singleEvents=True,
                )
                .execute()
            )
    except Exception as e:
        logger.error(f"❌ Error fetching events for {label}: {e}")
        return

    if "nextSyncToken" in events:
        save_secret(f"sync-token-{cal_id}", events["nextSyncToken"])

    for event in events.get("items", []):
        if event.get("status") == "cancelled":
            continue
        summary = event.get("summary", "No title")
        start = event["start"].get("dateTime", event["start"].get("date"))
        end = event["end"].get("dateTime", event["end"].get("date"))
        status = event.get("status", "CONFIRMED").upper()
        location = event.get("location", "No location")
        description = event.get("description", "")

        msg = (
            f"📅 {summary} ({status})\n"
            f"🕑 {start} → {end}\n"
            f"📍 {location}\n"
            f"📝 {description if description else '—'}\n"
            f"📂 Calendar: {label}"
        )
        await send_telegram(msg)


# --- Routes ---
@app.get("/")
def root():
    return {"status": "ok", "calendars": list(CALENDARS.values())}


@app.post("/webhook")
async def webhook(request: Request):
    resource_state = request.headers.get("X-Goog-Resource-State")
    channel_id = request.headers.get("X-Goog-Channel-ID")

    logger.info(f"📨 Webhook: state={resource_state}, channel={channel_id}")

    if resource_state == "sync":
        return {"ok": True, "msg": "initial sync ignored"}

    # Lookup calendar for this channel
    mapping_json = get_secret("calendar-channel-map")
    if not mapping_json:
        logger.error("❌ No channel mapping found in Secret Manager")
        return {"status": "error", "error": "channel mapping missing"}

    mapping = json.loads(mapping_json)
    cal_id = mapping.get(channel_id)
    if not cal_id:
        logger.warning(f"⚠️ Unknown channel_id {channel_id}")
        return {"ok": True, "msg": "unknown channel ignored"}

    label = CALENDARS.get(cal_id, cal_id) or ""
    await process_calendar(cal_id, label)

    return {"ok": True}


@app.get("/register")
def register_watch():
    results = {}
    mapping = {}
    for cal_id, label in CALENDARS.items():
        try:
            body = {
                "id": str(uuid.uuid4()),
                "type": "web_hook",
                "address": WEBHOOK_URL,
            }
            watch = (
                calendar_service.events().watch(calendarId=cal_id, body=body).execute()
            )
            results[label] = watch
            mapping[watch["id"]] = cal_id
            logger.info(f"✅ Registered watch for {label}")
        except Exception as e:
            logger.error(f"❌ Failed to register watch for {label}: {e}")

    # Save channel → calendar mapping in Secret Manager
    if mapping:
        save_secret("calendar-channel-map", json.dumps(mapping))

    return {"channels": results, "errors": None if results else "all failed"}

