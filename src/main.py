import os
import uuid
import logging
from datetime import datetime
from typing import Optional, Dict, Any
import httpx
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from google.oauth2 import service_account
from googleapiclient.discovery import build
from google.cloud import secretmanager_v1
from google.api_core.exceptions import NotFound
import asyncio
from pydantic import Field
from tenacity import retry, stop_after_attempt, wait_exponential
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("calendar-webhook")


# ---------------- Config ----------------
class Settings(BaseSettings):
    telegram_token: str
    telegram_chat_id: str
    gcp_project: str
    webhook_url: str
    calendar_ids: str = Field(..., alias="CALENDAR_IDS")
    google_application_credentials: str | None = None
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def parsed_calendars(self) -> dict[str, str]:
        """Parse CALENDAR_IDS env var into {calendar_id: label}"""
        parsed: dict[str, str] = {}
        for pair in self.calendar_ids.split(";"):
            if not pair.strip():
                continue
            try:
                cal_id, label = pair.split("|", 1)
                parsed[cal_id.strip()] = label.strip()
            except ValueError:
                logger.warning(f"⚠️ Invalid calendar config: {pair}")
        return parsed


settings = Settings()

logger.info(f"📅 Loaded calendars: {list(settings.parsed_calendars.values())}")

SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SERVICE_ACCOUNT_FILE = os.getenv(
    "GOOGLE_APPLICATION_CREDENTIALS", "/secrets/service-account.json"
)

credentials = service_account.Credentials.from_service_account_file(
    SERVICE_ACCOUNT_FILE, scopes=SCOPES
)

# ---------------- Globals ----------------
calendar_service = None
secret_client = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global calendar_service, secret_client
    calendar_service = build(
        "calendar", "v3", credentials=credentials, cache_discovery=False
    )
    secret_client = secretmanager_v1.SecretManagerServiceClient(credentials=credentials)
    logger.info("✅ Services initialized successfully")
    yield
    logger.info("🔄 Shutting down services")


app = FastAPI(
    title="Calendar Webhook Service",
    description="Google Calendar webhook integration with Telegram notifications",
    version="1.1.0",
    lifespan=lifespan,
)

# ---------------- Middleware ----------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://*.googleapis.com"],
    allow_credentials=True,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


# ---------------- Telegram ----------------
@retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=10), reraise=True)
async def send_telegram(text: str):
    """Send message to Telegram"""
    if len(text) > 4096:
        text = text[:4090] + "..."

    url = f"https://api.telegram.org/bot{settings.telegram_token}/sendMessage"
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
        )
        resp.raise_for_status()
        logger.info("✅ Telegram message sent")


# ---------------- Sync tokens ----------------
def get_sync_token(cal_id: str) -> Optional[str]:
    secret_name = f"projects/{settings.gcp_project}/secrets/calendar-sync-tokens-{cal_id}/versions/latest"
    try:
        response = secret_client.access_secret_version(name=secret_name)
        return response.payload.data.decode("utf-8")
    except NotFound:
        return None
    except Exception as e:
        logger.error(f"⚠️ Error retrieving sync token for {cal_id}: {e}")
        return None


def save_sync_token(cal_id: str, token: str):
    secret_id = f"calendar-sync-tokens-{cal_id}"
    parent = f"projects/{settings.gcp_project}"

    try:
        secret_client.get_secret(name=f"{parent}/secrets/{secret_id}")
    except NotFound:
        logger.info(f"🔐 Creating secret for {cal_id}")
        secret_client.create_secret(
            parent=parent,
            secret_id=secret_id,
            secret=secretmanager_v1.Secret(
                replication=secretmanager_v1.Replication(
                    automatic=secretmanager_v1.Replication.Automatic()
                )
            ),
        )

    secret_client.add_secret_version(
        parent=f"{parent}/secrets/{secret_id}",
        payload=secretmanager_v1.SecretPayload(data=token.encode("utf-8")),
    )


# ---------------- Events ----------------
def format_event_message(event: Dict[str, Any], calendar_label: str) -> str:
    summary = event.get("summary", "No title")
    status = event.get("status", "CONFIRMED").upper()
    location = event.get("location", "")
    description = event.get("description", "")

    start_raw = event["start"].get("dateTime") or event["start"].get("date")
    end_raw = event["end"].get("dateTime") or event["end"].get("date")

    try:
        if "T" in start_raw:
            start_dt = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(end_raw.replace("Z", "+00:00"))
            time_str = f"{start_dt:%Y-%m-%d %H:%M} → {end_dt:%H:%M}"
        else:
            time_str = f"All day: {start_raw}"
    except Exception:
        time_str = f"{start_raw} → {end_raw}"

    if description and len(description) > 200:
        description = description[:197] + "..."

    msg = f"<b>📅 {summary}</b>"
    if status != "CONFIRMED":
        msg += f" <i>({status})</i>"
    msg += f"\n🕑 {time_str}"
    if location:
        msg += f"\n📍 {location}"
    if description:
        msg += f"\n📝 {description}"
    msg += f"\n📂 <i>Calendar: {calendar_label}</i>"

    return msg


async def process_calendar_events(cal_id: str, label: str):
    sync_token = get_sync_token(cal_id)
    try:
        if sync_token:
            events_result = await asyncio.to_thread(
                lambda: calendar_service.events()
                .list(calendarId=cal_id, syncToken=sync_token, singleEvents=True)
                .execute()
            )
        else:
            events_result = await asyncio.to_thread(
                lambda: calendar_service.events()
                .list(
                    calendarId=cal_id,
                    maxResults=5,
                    orderBy="updated",
                    singleEvents=True,
                )
                .execute()
            )
    except Exception as e:
        logger.error(f"❌ Error fetching events: {e}")
        return

    if "nextSyncToken" in events_result:
        save_sync_token(cal_id, events_result["nextSyncToken"])

    for event in events_result.get("items", []):
        try:
            msg = format_event_message(event, label)
            await send_telegram(msg)
            await asyncio.sleep(0.2)
        except Exception as e:
            logger.error(f"❌ Error sending event {event.get('id')}: {e}")


# ---------------- Routes ----------------
@app.get("/")
def root():
    return {"status": "ok", "calendars": list(settings.parsed_calendars.values())}


@app.get("/health")
def health():
    try:
        calendar_service.calendarList().list(maxResults=1).execute()
        return {"status": "ok", "checked": True}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    state = request.headers.get("X-Goog-Resource-State")
    if state == "sync":
        return {"status": "ok", "message": "sync ignored"}
    for cal_id, label in settings.parsed_calendars.items():
        background_tasks.add_task(process_calendar_events, cal_id, label)
    return {"status": "ok", "message": "processing events"}


@app.get("/register")
async def register():
    results = []
    for cal_id, label in settings.parsed_calendars.items():
        body = {
            "id": str(uuid.uuid4()),
            "type": "web_hook",
            "address": settings.webhook_url,
            "expiration": str(int((datetime.utcnow().timestamp() + 86400 * 7) * 1000)),
        }
        watch = calendar_service.events().watch(calendarId=cal_id, body=body).execute()
        results.append({"calendar": label, "channel_id": watch.get("id")})
    return {"channels": results}


@app.get("/test-telegram")
async def test_telegram():
    await send_telegram("🧪 Test message from Calendar Webhook Service")
    return {"status": "ok"}
