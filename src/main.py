import os
import uuid
import httpx
from fastapi import FastAPI, Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import secretmanager
from google.cloud import secretmanager_v1


app = FastAPI()

# --- Config from env ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# CALENDAR_IDS is like: id1|Rubina;id2|Zara
RAW_CALENDAR_IDS = os.getenv("CALENDAR_IDS", "")
CALENDARS = {}
for pair in RAW_CALENDAR_IDS.split(";"):
    if not pair.strip():
        continue
    cal_id, label = pair.split("|", 1)
    CALENDARS[cal_id] = label

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS", "/secrets/service-account.json"
)

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)


def get_calendar_service():
    return build("calendar", "v3", credentials=credentials)


def get_secret_manager_client():
    return secretmanager.SecretManagerServiceClient(credentials=credentials)


async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})


def get_sync_token(cal_id: str) -> str | None:
    """Fetch last sync token from Secret Manager for this calendar."""
    client = get_secret_manager_client()
    secret_name = f"projects/{os.getenv('GCP_PROJECT')}/secrets/calendar-sync-tokens-{cal_id}/versions/latest"
    try:
        response = client.access_secret_version(name=secret_name)
        return response.payload.data.decode("utf-8")
    except Exception as e:
        print(f"⚠️ No sync token yet for {cal_id}: {e}")
        return None


def save_sync_token(cal_id: str, token: str):
    """Save sync token to Secret Manager."""
    client = secretmanager_v1.SecretManagerServiceClient(credentials=credentials)
    secret_id = f"calendar-sync-tokens-{cal_id}"
    parent = f"projects/{os.getenv('GCP_PROJECT')}"

    try:
        # Ensure secret exists
        client.get_secret(name=f"{parent}/secrets/{secret_id}")
    except Exception:
        # Create the secret with automatic replication
        client.create_secret(
            parent=parent,
            secret_id=secret_id,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                )
            ),
        )

    # Add new version with the sync token
    client.add_secret_version(
        parent=f"{parent}/secrets/{secret_id}",
        payload=secretmanager_v1.SecretPayload(data=token.encode("utf-8")),
    )


@app.get("/")
def read_root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    print(f"Webhook received for channel {channel_id} with state {resource_state}")

    # Ignore sync events (initial handshake)
    if resource_state == "sync":
        print("ℹ️ Ignoring initial sync event")
        return {"ok": True}

    service = get_calendar_service()

    for cal_id, label in CALENDARS.items():
        sync_token = get_sync_token(cal_id)
        try:
            if sync_token:
                events = (
                    service.events()
                    .list(calendarId=cal_id, syncToken=sync_token, singleEvents=True)
                    .execute()
                )
            else:
                # Full fetch if no token yet
                events = (
                    service.events()
                    .list(
                        calendarId=cal_id,
                        maxResults=5,
                        orderBy="updated",
                        singleEvents=True,
                    )
                    .execute()
                )
        except Exception as e:
            print(f"⚠️ Error fetching events for {label}: {e}")
            continue

        # Save next sync token
        if "nextSyncToken" in events:
            save_sync_token(cal_id, events["nextSyncToken"])

        for event in events.get("items", []):
            summary = event.get("summary", "No title")
            start = event["start"].get("dateTime", event["start"].get("date"))
            end = event["end"].get("dateTime", event["end"].get("date"))
            status = event.get("status", "CONFIRMED").upper()
            location = event.get("location", "No location")
            description = event.get("description", "No description")

            msg = (
                f"📅 {summary} ({status})\n"
                f"🕑 {start} → {end}\n"
                f"📍 {location}\n"
                f"📝 {description}\n"
                f"📂 Calendar: {label}"
            )
            await send_telegram(msg)

    return {"ok": True}


@app.get("/register")
def register_watch():
    """Register a watch channel for each calendar."""
    service = get_calendar_service()
    results = []
    for cal_id, label in CALENDARS.items():
        body = {
            "id": str(uuid.uuid4()),
            "type": "web_hook",
            "address": os.getenv("WEBHOOK_URL"),
        }
        print(f"📌 Registering watch for calendar: {label} ({cal_id})")
        watch = service.events().watch(calendarId=cal_id, body=body).execute()
        results.append({"label": label, "watch": watch})
    return {"channels": results}
