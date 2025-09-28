import os
import uuid
import httpx
import json
from fastapi import FastAPI, Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import secretmanager

app = FastAPI()

# --- Config from env ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

CALENDARS_ENV = os.getenv("CALENDAR_IDS", "")
ONLY_IDS = [c.split("|")[0] for c in CALENDARS_ENV.split(";") if c]
CALENDAR_LABELS = {
    c.split("|")[0]: c.split("|")[1] for c in CALENDARS_ENV.split(";") if "|" in c
}

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS", "/secrets/service-account.json"
)

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

secret_client = secretmanager.SecretManagerServiceClient()
PROJECT_ID = os.getenv("GCP_PROJECT")
SECRET_NAME = "calendar-sync-tokens"


def get_calendar_service():
    return build("calendar", "v3", credentials=credentials)


def load_sync_tokens():
    try:
        name = f"projects/{PROJECT_ID}/secrets/{SECRET_NAME}/versions/latest"
        response = secret_client.access_secret_version(request={"name": name})
        return json.loads(response.payload.data.decode("utf-8"))
    except Exception as e:
        print(f"No existing sync tokens found: {e}")
        return {}


def save_sync_tokens(tokens: dict):
    payload = json.dumps(tokens)
    parent = f"projects/{PROJECT_ID}/secrets/{SECRET_NAME}"
    secret_client.add_secret_version(
        request={"parent": parent, "payload": {"data": payload.encode("utf-8")}}
    )


async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})


@app.get("/")
def read_root():
    return {"status": "ok"}


# --- Load tokens at startup ---
sync_tokens = load_sync_tokens()


@app.post("/webhook")
async def webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    print(f"Webhook received for channel {channel_id} with state {resource_state}")

    if resource_state == "exists":
        service = get_calendar_service()
        changed = False

        for cal_id in ONLY_IDS:
            try:
                if cal_id in sync_tokens:
                    events_result = (
                        service.events()
                        .list(
                            calendarId=cal_id,
                            syncToken=sync_tokens[cal_id],
                            singleEvents=True,
                        )
                        .execute()
                    )
                else:
                    events_result = (
                        service.events()
                        .list(
                            calendarId=cal_id,
                            maxResults=5,
                            singleEvents=True,
                            orderBy="updated",
                        )
                        .execute()
                    )

                for event in events_result.get("items", []):
                    summary = event.get("summary", "No title")
                    start = event["start"].get("dateTime", event["start"].get("date"))
                    end = event["end"].get("dateTime", event["end"].get("date"))
                    location = event.get("location", "No location")
                    description = event.get("description", "No description")
                    status = event.get("status", "unknown").upper()
                    label = CALENDAR_LABELS.get(cal_id, cal_id)

                    msg = (
                        f"📅 {summary} ({status})\n"
                        f"🕑 {start} → {end}\n"
                        f"📍 {location}\n"
                        f"📝 {description}\n"
                        f"📂 Calendar: {label}"
                    )
                    await send_telegram(msg)

                if "nextSyncToken" in events_result:
                    sync_tokens[cal_id] = events_result["nextSyncToken"]
                    changed = True

            except Exception as e:
                print(f"Error fetching events for {cal_id}: {e}")

        if changed:
            save_sync_tokens(sync_tokens)

    elif resource_state == "not_exists":
        await send_telegram("❌ Appointment deleted")

    return {"ok": True}


@app.get("/register")
def register_watch():
    """Register a watch channel for each calendar (call this once or via Cloud Scheduler)."""
    service = get_calendar_service()
    results = []
    changed = False

    for cal_id in ONLY_IDS:
        body = {
            "id": str(uuid.uuid4()),
            "type": "web_hook",
            "address": os.getenv("WEBHOOK_URL"),
        }
        watch = service.events().watch(calendarId=cal_id, body=body).execute()
        results.append(watch)

        init_sync = (
            service.events().list(calendarId=cal_id, singleEvents=True).execute()
        )
        if "nextSyncToken" in init_sync:
            sync_tokens[cal_id] = init_sync["nextSyncToken"]
            changed = True

    if changed:
        save_sync_tokens(sync_tokens)

    return {"channels": results, "sync_tokens": sync_tokens}
