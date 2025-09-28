import os
import uuid
import httpx
from fastapi import FastAPI, Request
from google.oauth2 import service_account
from googleapiclient.discovery import build

app = FastAPI()

# --- Config from env ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
CALENDAR_IDS = os.getenv("CALENDAR_IDS", "").split(",")  # comma-separated list

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "/secrets/service-account.json")

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)


def get_calendar_service():
    return build("calendar", "v3", credentials=credentials)

async def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})

@app.get("/")
def read_root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    # Google headers
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    print(f"Webhook received for channel {channel_id} with state {resource_state}")

    # New/updated events
    if resource_state == "exists":
        service = get_calendar_service()
        for cal_id in CALENDAR_IDS:
            events = (
                service.events()
                .list(
                    calendarId=cal_id,
                    maxResults=1,
                    orderBy="updated",
                    singleEvents=True,
                )
                .execute()
            )
            if not events.get("items"):
                continue
            event = events["items"][0]
            summary = event.get("summary", "No title")
            start = event["start"].get("dateTime", event["start"].get("date"))
            status = event.get("status")
            msg = f"📅 {status.upper()} → {summary}\n🕑 {start}"
            await send_telegram(msg)

    elif resource_state == "not_exists":
        await send_telegram("❌ Appointment deleted")

    return {"ok": True}

@app.get("/register")
def register_watch():
    """Register a watch channel for each calendar (call this once or via Cloud Scheduler)."""
    service = get_calendar_service()
    results = []
    for cal_id in CALENDAR_IDS:
        body = {
            "id": str(uuid.uuid4()),  # unique channel id
            "type": "web_hook",
            "address": os.getenv("WEBHOOK_URL"),
        }
        watch = service.events().watch(calendarId=cal_id, body=body).execute()
        results.append(watch)
    return {"channels": results}
