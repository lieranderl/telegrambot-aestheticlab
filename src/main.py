import os
import uuid
import logging
from typing import Optional
from googleapiclient.errors import HttpError

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

# Prefer GOOGLE_CLOUD_PROJECT env, fallback to ADC project
credentials, adc_project = google_auth_default(scopes=SCOPES)
GCP_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT") or adc_project
if not GCP_PROJECT:
    raise RuntimeError("❌ Could not resolve GCP project ID")

logger.info(f"🔑 Using ADC with project: {GCP_PROJECT}")

calendar_service = build(
    "calendar", "v3", credentials=credentials, cache_discovery=False
)
secret_client = secretmanager_v1.SecretManagerServiceClient(credentials=credentials)


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
        f"projects/{GCP_PROJECT}/secrets/calendar-sync-tokens-{cal_id}/versions/latest"
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
    parent = f"projects/{GCP_PROJECT}"

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
        logger.error(f"❌ Failed to create secret {secret_id}: {e}")
        return

    try:
        secret_client.add_secret_version(
            parent=f"{parent}/secrets/{secret_id}",
            payload=secretmanager_v1.SecretPayload(data=token.encode("utf-8")),
        )
        logger.info(f"💾 Saved sync token for {cal_id}")
    except Exception as e:
        logger.error(f"❌ Failed to save sync token: {e}")


# --- Routes ---


@app.get("/health")
def health():
    """Basic health check"""
    try:
        calendar_service.calendarList().list(maxResults=1).execute()
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/webhook")
async def webhook(request: Request):
    resource_state = request.headers.get("X-Goog-Resource-State")
    logger.info(f"📨 Webhook received: state={resource_state}")

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
    """Register webhooks for all calendars"""
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

        except HttpError as e:
            if e.resp.status == 404:
                err_msg = (
                    f"❌ Calendar not found or not shared: {label} ({cal_id}). "
                    f"Share this calendar with the service account: "
                    f"{credentials.service_account_email}"
                )
                logger.error(err_msg)
                errors.append({"label": label, "error": err_msg})
            else:
                logger.error(f"❌ Failed to register watch for {label}: {e}")
                errors.append({"label": label, "error": str(e)})

        except Exception as e:
            logger.error(f"❌ Unexpected error for {label}: {e}")
            errors.append({"label": label, "error": str(e)})

    return {"channels": results, "errors": errors if errors else None}
