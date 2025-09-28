import os
import uuid
import hashlib
import logging
from typing import Optional, Dict, Tuple, List

import httpx
from fastapi import FastAPI, Request, HTTPException
from googleapiclient.discovery import build
from google.api_core.exceptions import NotFound, AlreadyExists
from google.cloud import secretmanager_v1
from google.auth import default as google_auth_default

# ---------------- Logging ----------------
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Calendar→Telegram webhook")

# ---------------- Config ----------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
RAW_CALENDARS = os.getenv("CALENDAR_IDS", "")  # "id1|Label1;id2|Label2"

if not all([TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_URL, RAW_CALENDARS]):
    raise RuntimeError(
        "Missing required env: TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, WEBHOOK_URL, CALENDAR_IDS"
    )

# Parse calendars: id1|Label1;id2|Label2
CALENDARS: Dict[str, str] = {}
for pair in RAW_CALENDARS.split(";"):
    pair = pair.strip()
    if not pair:
        continue
    try:
        cal_id, label = pair.split("|", 1)
        CALENDARS[cal_id.strip()] = label.strip()
    except ValueError:
        logger.warning(f"Invalid CALENDAR_IDS entry (ignored): {pair}")

logger.info(f"Configured calendars: {CALENDARS}")

# ---------------- Google clients (ADC with proper scopes) ----------------
BASE_CREDS, PROJECT_ID = google_auth_default()  # no scopes here
if not PROJECT_ID:
    # Fall back to env if metadata isn't available for any reason
    PROJECT_ID = os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("GCP_PROJECT")
logger.info(f"Using project: {PROJECT_ID}")

# Per-API scoped creds
CAL_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
SM_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

cal_creds = (
    BASE_CREDS.with_scopes(CAL_SCOPES)
    if hasattr(BASE_CREDS, "with_scopes")
    else BASE_CREDS
)
sm_creds = (
    BASE_CREDS.with_scopes(SM_SCOPES)
    if hasattr(BASE_CREDS, "with_scopes")
    else BASE_CREDS
)

calendar_service = build("calendar", "v3", credentials=cal_creds, cache_discovery=False)
secret_client = secretmanager_v1.SecretManagerServiceClient(credentials=sm_creds)

# Secret IDs (constant names)
CHANNEL_MAP_SECRET_ID = "calendar-channel-map"  # stores lines: channel_id|cal_id|label


# ---------------- Secret helpers ----------------
def _safe_suffix_from_cal_id(cal_id: str) -> str:
    """Secret IDs must match ^[A-Za-z0-9_-]+$, so hash the calendarId for use in secret names."""
    return hashlib.sha1(cal_id.encode("utf-8")).hexdigest()  # 40 hex chars


def _sync_secret_id_for(cal_id: str) -> str:
    return f"cal-sync-{_safe_suffix_from_cal_id(cal_id)}"


def _secret_full_name(secret_id: str) -> str:
    return f"projects/{PROJECT_ID}/secrets/{secret_id}"


def _read_secret_text(secret_id: str) -> Optional[str]:
    name = f"{_secret_full_name(secret_id)}/versions/latest"
    try:
        resp = secret_client.access_secret_version(name=name)
        return resp.payload.data.decode("utf-8")
    except NotFound:
        return None


def _create_secret_if_missing(secret_id: str) -> None:
    parent = f"projects/{PROJECT_ID}"
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
        logger.info(f"Created secret: {secret_id}")
    except AlreadyExists:
        pass


def _write_secret_text(secret_id: str, text: str) -> None:
    _create_secret_if_missing(secret_id)
    parent = _secret_full_name(secret_id)
    secret_client.add_secret_version(
        parent=parent, payload=secretmanager_v1.SecretPayload(data=text.encode("utf-8"))
    )


def _upsert_channel_mapping(channel_id: str, cal_id: str, label: str) -> None:
    """Store/refresh channel mapping in one secret as newline-delimited lines."""
    current = _read_secret_text(CHANNEL_MAP_SECRET_ID) or ""
    lines = [ln for ln in current.splitlines() if ln.strip()]
    # remove any existing mapping for this channel
    lines = [ln for ln in lines if not ln.startswith(f"{channel_id}|")]
    lines.append(f"{channel_id}|{cal_id}|{label}")
    _write_secret_text(CHANNEL_MAP_SECRET_ID, "\n".join(lines))
    logger.info(f"Saved channel mapping: {channel_id}|{cal_id}|{label}")


def _lookup_channel(channel_id: str) -> Optional[Tuple[str, str]]:
    """Return (cal_id, label) for a channel_id, or None if not mapped."""
    data = _read_secret_text(CHANNEL_MAP_SECRET_ID)
    if not data:
        return None
    for ln in data.splitlines():
        parts = ln.strip().split("|", 2)
        if len(parts) == 3 and parts[0] == channel_id:
            return parts[1], parts[2]
    return None


def _get_sync_token(cal_id: str) -> Optional[str]:
    sec_id = _sync_secret_id_for(cal_id)
    return _read_secret_text(sec_id)


def _save_sync_token(cal_id: str, token: str) -> None:
    sec_id = _sync_secret_id_for(cal_id)
    _write_secret_text(sec_id, token)
    logger.info(f"Saved sync token for {cal_id[:8]}…")


# ---------------- Telegram ----------------
async def send_telegram(text: str) -> None:
    if not text:
        return
    if len(text) > 4096:
        text = text[:4090] + "…"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": text})
        r.raise_for_status()


# ---------------- Calendar fetchers ----------------
def _initial_seed_sync_token(cal_id: str) -> None:
    """
    Full sync to obtain nextSyncToken WITHOUT sending any messages.
    We must paginate until 'nextSyncToken' shows up.
    """
    page_token = None
    while True:
        resp = (
            calendar_service.events()
            .list(
                calendarId=cal_id,
                singleEvents=True,
                showDeleted=True,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        page_token = resp.get("nextPageToken")
        if not page_token:
            # last page – should include nextSyncToken
            nst = resp.get("nextSyncToken")
            if nst:
                _save_sync_token(cal_id, nst)
            return


def _delta_changes(cal_id: str, sync_token: str) -> Dict:
    """Fetch delta changes using syncToken. May raise HttpError(410) if token invalid."""
    # We still paginate in case of many changes in one burst
    aggregated_items: List[Dict] = []
    page_token = None
    last_resp = None
    while True:
        resp = (
            calendar_service.events()
            .list(
                calendarId=cal_id,
                singleEvents=True,
                showDeleted=True,
                syncToken=sync_token,
                maxResults=2500,
                pageToken=page_token,
            )
            .execute()
        )
        last_resp = resp
        aggregated_items.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return {
        "items": aggregated_items,
        "nextSyncToken": (last_resp or {}).get("nextSyncToken"),
    }


def _format_event_message(event: Dict, label: str) -> Optional[str]:
    # skip “birthday / workingLocation / OOO” kinds if you wish (optional)
    summary = event.get("summary") or "No title"
    status = (event.get("status") or "confirmed").upper()
    if event.get("status") == "cancelled":
        return f"❌ Event cancelled\n📂 {label}\n🆔 {event.get('id')}"

    start = event.get("start", {})
    end = event.get("end", {})
    start_val = start.get("dateTime") or start.get("date") or "?"
    end_val = end.get("dateTime") or end.get("date") or "?"

    loc = event.get("location") or "—"
    desc = event.get("description") or "—"

    return (
        f"📅 {summary} ({status})\n"
        f"🕑 {start_val} → {end_val}\n"
        f"📍 {loc}\n"
        f"📝 {desc}\n"
        f"📂 Calendar: {label}"
    )


# ---------------- Routes ----------------
@app.get("/")
def root():
    return {
        "status": "ok",
        "project": PROJECT_ID,
        "calendars": list(CALENDARS.values()),
    }


@app.get("/register")
def register_watch():
    """
    Register a watch channel for each configured calendar,
    and persist channel→calendar mapping so /webhook can target only the changed calendar.
    """
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
            channel_id = watch.get("id")
            _upsert_channel_mapping(channel_id, cal_id, label)
            results.append({"label": label, "watch": watch})
            logger.info(f"Registered watch for {label}")
        except Exception as e:
            msg = f"Calendar not found or not shared: {label} ({cal_id}). {e}"
            logger.error(msg)
            errors.append({"label": label, "error": msg})
    return {"channels": results, "errors": errors or None}


@app.post("/webhook")
async def webhook(request: Request):
    """
    Handle Google push notifications. We process ONLY the calendar that sent this channel.
    - On first notification (no sync token): seed nextSyncToken, send NOTHING
    - On deltas: send concise Telegram messages
    """
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get(
        "X-Goog-Resource-State"
    )  # "exists", "sync", "not_exists"
    logger.info(f"Webhook: state={resource_state}, channel={channel_id}")

    # Ignore if headers missing
    if not channel_id or not resource_state:
        raise HTTPException(status_code=400, detail="Missing X-Goog- headers")

    # Map the channel to a calendar
    mapped = _lookup_channel(channel_id)
    if not mapped:
        logger.warning(f"No channel mapping found for {channel_id} (register again?)")
        return {"status": "ok", "msg": "unknown channel"}

    cal_id, label = mapped

    # Ignore initial sync handshake
    if resource_state == "sync":
        return {"status": "ok", "msg": "sync handshake ignored"}

    # If we have no sync token yet: do an initial seed to get nextSyncToken, send nothing
    token = _get_sync_token(cal_id)
    if not token:
        logger.info(f"No sync token for {label}. Seeding without notifications…")
        _initial_seed_sync_token(cal_id)
        return {"status": "ok", "msg": "seeded sync token"}

    # We have a token → pull deltas
    try:
        result = _delta_changes(cal_id, token)
    except Exception as e:
        # If token invalid/expired (HttpError 410), re-seed token (still no messages)
        msg = str(e)
        if "410" in msg or "syncToken" in msg.lower():
            logger.warning(f"Sync token invalid for {label}. Re-seeding…")
            _initial_seed_sync_token(cal_id)
            return {"status": "ok", "msg": "reseeded"}
        logger.error(f"Error fetching deltas for {label}: {e}")
        return {"status": "error", "error": str(e)}

    # Update token
    if result.get("nextSyncToken"):
        _save_sync_token(cal_id, result["nextSyncToken"])

    # Send messages for each changed item
    sent = 0
    for ev in result.get("items", []):
        msg = _format_event_message(ev, label)
        if not msg:
            continue
        try:
            await send_telegram(msg)
            sent += 1
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")

    return {"status": "ok", "sent": sent}


@app.get("/test-telegram")
async def test_tg():
    try:
        await send_telegram("🧪 Telegram test OK")
        return {"status": "ok"}
    except Exception as e:
        return {"status": "error", "error": str(e)}
