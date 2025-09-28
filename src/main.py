import os
import uuid
import logging
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
CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]

# Calendar service with explicit scopes
calendar_credentials, project_id = google_auth_default(scopes=CALENDAR_SCOPES)
calendar_service = build(
    "calendar", "v3", credentials=calendar_credentials, cache_discovery=False
)

# Secret Manager with full IAM (no scope restriction)
sm_credentials, _ = google_auth_default()
secret_client = secretmanager_v1.SecretManagerServiceClient(credentials=sm_credentials)

logger.info(f"🔑 Using ADC. Effective project: {project_id}")


# --- Helpers ---
async def send_telegram(text: str):
    """Send message to Telegram with error handling"""
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


def get_sync_token(cal_id: str) -> Optional[str]:
    """Get sync token from Secret Manager"""
    secret_name = (
        f"projects/{project_id}/secrets/calendar-sync-tokens-{cal_id}/versions/latest"
    )
    try:
        response = secret_client.access_secret_version(name=secret_name)
        return response.payload.data.decode("utf-8")
    except NotFound:
        logger.info(f"ℹ️ No sync token found for {cal_id}")
        return None
    except Exception as e:
        logger.error(f"⚠️ Error fetching sync token for {cal_id}: {e}")
        return None


def save_sync_token(cal_id: str, token: str):
    """Save sync token to Secret Manager"""
    secret_id = f"calendar-sync-tokens-{cal_id}"
    parent = f"projects/{project_id}"

    try:
        secret_client.create_secret(
            parent=parent,
            secret_id=secret_id,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                )
            ),
        )
        logger.info(f"🔐 Created new secret for {cal_id}")
    except AlreadyExists:
        pass
    except Exception as e:
        logger.warning(f"⚠️ Cannot create secret {secret_id}: {e}")
        return

    try:
        secret_client.add_secret_version(
            parent=f"{parent}/secrets/{secret_id}",
            payload=secretmanager_v1.SecretPayload(data=token.encode("utf-8")),
        )
        logger.info(f"💾 Saved sync token for {cal_id}")
    except Exception as e:
        logger.warning(f"⚠️ Cannot save sync token for {cal_id}: {e}")


def save_channel_mapping(channel_id: str, cal_id: str, label: str):
    """Save channel_id → calendar mapping in Secret Manager"""
    secret_id = "calendar-channel-map"
    parent = f"projects/{project_id}"
    payload = f"{channel_id}|{cal_id}|{label}"

    try:
        # Try creating the secret if not exists
        secret_client.create_secret(
            parent=parent,
            secret_id=secret_id,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                )
            ),
        )
        logger.info(f"🔐 Created secret {secret_id}")
    except AlreadyExists:
        pass
    except Exception as e:
        logger.warning(f"⚠️ Cannot create secret {secret_id}: {e}")
        return

    try:
        secret_client.add_secret_version(
            parent=f"{parent}/secrets/{secret_id}",
            payload=secretmanager_v1.SecretPayload(data=payload.encode("utf-8")),
        )
        logger.info(f"💾 Saved channel mapping: {payload}")
    except Exception as e:
        logger.warning(f"⚠️ Cannot save channel mapping: {e}")


# --- Routes ---
@app.get("/health")
def root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    resource_state = request.headers.get("X-Goog-Resource-State")
    channel_id = request.headers.get("X-Goog-Channel-ID")
    logger.info(f"📨 Webhook received: state={resource_state}, channel={channel_id}")

    if resource_state == "sync":
        return {"ok": True, "msg": "initial sync ignored"}

    for cal_id, label in CALENDARS.items():
        sync_token = get_sync_token(cal_id)
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
            continue

        if "nextSyncToken" in events:
            save_sync_token(cal_id, events["nextSyncToken"])

        for event in events.get("items", []):
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

    return {"ok": True}


@app.get("/register")
def register_watch():
    results = []
    errors = []

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
            results.append({"label": label, "watch": watch})
            logger.info(f"✅ Registered watch for {label}")

            # Save mapping channel_id → calendar
            channel_id = watch.get("id")
            if channel_id:
                save_channel_mapping(channel_id, cal_id, label)

        except Exception as e:
            error_msg = f"❌ Failed to register watch for {label}: {e}"
            logger.error(error_msg)
            errors.append({"label": label, "error": error_msg})

    return {"channels": results, "errors": errors or None}
