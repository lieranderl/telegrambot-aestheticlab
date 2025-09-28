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

# 🔖 CALENDAR_IDS env: id|label;id|label
raw_calendars = os.getenv("CALENDAR_IDS", "")
CALENDAR_LABELS = {}
ONLY_IDS = []

if raw_calendars:
    for entry in raw_calendars.split(";"):
        if "|" in entry:
            cal_id, label = entry.split("|", 1)
            cal_id = cal_id.strip()
            ONLY_IDS.append(cal_id)
            CALENDAR_LABELS[cal_id] = label.strip()
        else:
            cal_id = entry.strip()
            ONLY_IDS.append(cal_id)
            CALENDAR_LABELS[cal_id] = cal_id  # fallback label

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
        await client.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"},
        )


@app.get("/")
def read_root():
    return {"status": "ok"}

@app.get("/debug")
def debug():
    return {
        "env_CALENDAR_IDS": raw_calendars,
        "ONLY_IDS": ONLY_IDS,
        "CALENDAR_LABELS": CALENDAR_LABELS,
    }



@app.post("/webhook")
async def webhook(request: Request):
    # Google headers
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    print(f"Webhook received for channel {channel_id} with state {resource_state}")

    if resource_state == "exists":
        service = get_calendar_service()
        for cal_id in ONLY_IDS:
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
            end = event["end"].get("dateTime", event["end"].get("date"))
            status = event.get("status", "unknown").upper()
            location = event.get("location", "No location")
            description = event.get("description", "No description")
            calendar_name = CALENDAR_LABELS.get(cal_id, cal_id)

            msg = (
                f"📅 *{summary}* ({status})\n"
                f"🕑 {start} → {end}\n"
                f"📍 {location}\n"
                f"📝 {description}\n"
                f"📂 Calendar: *{calendar_name}*"
            )
            await send_telegram(msg)

    elif resource_state == "not_exists":
        await send_telegram("❌ Appointment deleted")

    return {"ok": True}


@app.get("/register")
def register_watch():
    """Register a watch channel for each calendar (call this once or via Cloud Scheduler)."""
    service = get_calendar_service()
    results = []
    for cal_id in ONLY_IDS:
        body = {
            "id": str(uuid.uuid4()),
            "type": "web_hook",
            "address": os.getenv("WEBHOOK_URL"),
        }
        watch = service.events().watch(calendarId=cal_id, body=body).execute()
        results.append(
            {
                "calendarId": cal_id,
                "calendarName": CALENDAR_LABELS.get(cal_id, cal_id),
                "watch": watch,
            }
        )
    return {"channels": results}
