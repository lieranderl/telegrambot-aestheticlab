import os
import uuid
import logging
from typing import Optional

import httpx
from fastapi import FastAPI, Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import secretmanager_v1
from google.api_core.exceptions import NotFound, AlreadyExists
from google.protobuf.timestamp_pb2 import Timestamp
from datetime import datetime, timezone

# --- Logging ---
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI()

# --- Config ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
GCP_PROJECT = os.getenv("GCP_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, GCP_PROJECT, WEBHOOK_URL]):
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
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS", "/secrets/service-account.json"
)

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)
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


def ensure_secret_exists(secret_id: str):
    parent = f"projects/{GCP_PROJECT}"
    try:
        secret_client.get_secret(name=f"{parent}/secrets/{secret_id}")
        return
    except NotFound:
        logger.info(f"ℹ️ Secret {secret_id} not found, creating...")
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
        logger.info(f"🔐 Created secret {secret_id}")
    except AlreadyExists:
        pass


def save_secret_value(secret_id: str, value: str):
    ensure_secret_exists(secret_id)
    parent = f"projects/{GCP_PROJECT}/secrets/{secret_id}"
    try:
        secret_client.add_secret_version(
            parent=parent,
            payload=secretmanager_v1.SecretPayload(data=value.encode("utf-8")),
        )
        logger.info(f"💾 Saved new version for secret {secret_id}")
    except Exception as e:
        logger.error(f"❌ Failed to save secret {secret_id}: {e}")


def get_secret_value(secret_id: str) -> Optional[str]:
    name = f"projects/{GCP_PROJECT}/secrets/{secret_id}/versions/latest"
    try:
        response = secret_client.access_secret_version(name=name)
        return response.payload.data.decode("utf-8")
    except NotFound:
        return None
    except Exception as e:
        logger.error(f"⚠️ Failed to get secret {secret_id}: {e}")
        return None


# --- Sync Token helpers ---
def get_sync_token(cal_id: str) -> Optional[str]:
    return get_secret_value(f"calendar-sync-tokens-{cal_id}")


def save_sync_token(cal_id: str, token: str):
    save_secret_value(f"calendar-sync-tokens-{cal_id}", token)


# --- Resource mapping helpers ---
def save_resource_mapping(resource_id: str, cal_id: str):
    """Store mapping resourceId -> calendarId"""
    save_secret_value(f"calendar-resource-{resource_id}", cal_id)


def get_calendar_from_resource(resource_id: str) -> Optional[str]:
    return get_secret_value(f"calendar-resource-{resource_id}")


# --- Routes ---
@app.get("/")
def root():
    return {"status": "ok", "calendars": list(CALENDARS.values())}


@app.post("/webhook")
async def webhook(request: Request):
    resource_state = request.headers.get("X-Goog-Resource-State")
    resource_id = request.headers.get("X-Goog-Resource-ID") or ""
    logger.info(f"📨 Webhook received: state={resource_state}, resource={resource_id}")

    if resource_state == "sync":
        return {"ok": True, "msg": "initial sync ignored"}

    cal_id = get_calendar_from_resource(resource_id)
    if not cal_id:
        logger.warning(f"⚠️ No calendar mapping for resource {resource_id}")
        return {"ok": False, "msg": "unknown resource"}

    label = CALENDARS.get(cal_id, cal_id)
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
                    maxResults=10,
                    orderBy="updated",
                    singleEvents=True,
                )
                .execute()
            )
    except Exception as e:
        logger.error(f"❌ Error fetching events for {label}: {e}")
        return {"ok": False, "error": str(e)}

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
            resource_id = watch.get("resourceId")
            if resource_id:
                save_resource_mapping(resource_id, cal_id)

            results.append({"label": label, "watch": watch})
            logger.info(f"✅ Registered watch for {label} (resourceId={resource_id})")
        except Exception as e:
            logger.error(f"❌ Failed to register watch for {label}: {e}")
    return {"channels": results}


@app.get("/cleanup")
def cleanup_resource_mappings():
    """
    Remove stale resourceId → calendarId secrets.
    Deletes calendar-resource-* secrets older than 10 days.
    """
    parent = f"projects/{GCP_PROJECT}"
    deleted = []
    kept = []

    try:
        for secret in secret_client.list_secrets(request={"parent": parent}):
            name = secret.name.split("/")[-1]  # secret_id
            if not name.startswith("calendar-resource-"):
                continue

            create_time = secret.create_time
            if isinstance(create_time, Timestamp):
                # Convert protobuf timestamp to Python datetime
                create_dt = create_time.ToDatetime().replace(tzinfo=timezone.utc)
                age_days = (datetime.now(timezone.utc) - create_dt).days
            else:
                kept.append(name)
                continue

            if age_days > 10:  # stale
                try:
                    secret_client.delete_secret(name=secret.name)
                    deleted.append(name)
                    logger.info(
                        f"🗑️ Deleted stale mapping secret: {name} (age={age_days}d)"
                    )
                except Exception as e:
                    logger.error(f"❌ Failed to delete secret {name}: {e}")
                    kept.append(name)
            else:
                kept.append(name)

        return {"status": "ok", "deleted": deleted, "kept": kept}

    except Exception as e:
        logger.error(f"❌ Cleanup failed: {e}")
        return {"status": "error", "error": str(e)}
