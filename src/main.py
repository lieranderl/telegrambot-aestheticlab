import os
import uuid
import hashlib
import logging
from typing import Optional, Dict, Tuple, List
from datetime import datetime

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
    return hashlib.sha1(cal_id.encode("utf-8")).hexdigest()


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
    """Write text to Secret Manager, keeping only the latest version (safe + cost-optimized)."""
    _create_secret_if_missing(secret_id)
    parent = _secret_full_name(secret_id)
    safe_text = text.strip() or "{}"

    # Add a new version
    new_version = secret_client.add_secret_version(
        parent=parent,
        payload=secretmanager_v1.SecretPayload(data=safe_text.encode("utf-8")),
    )

    # List all versions(to clean up older ones)
    versions = list(secret_client.list_secret_versions(request={"parent": parent}))
    for v in versions:
        # Skip the one we just created
        if v.name == new_version.name:
            continue

        # Check state: only destroy ACTIVE versions
        try:
            state = v.state.name if hasattr(v, "state") else None
            if state and state.upper() == "DESTROYED":
                continue  # already gone, skip
            secret_client.destroy_secret_version(name=v.name)
            logger.info(f"Destroyed old secret version: {v.name}")
        except Exception as e:
            logger.warning(f"Failed to destroy {v.name}: {e}")

    logger.info(f"Updated secret {secret_id} → kept only {new_version.name}")


def _get_sync_token(cal_id: str) -> Optional[str]:
    sec_id = _sync_secret_id_for(cal_id)
    return _read_secret_text(sec_id)


def _save_sync_token(cal_id: str, token: str) -> None:
    sec_id = _sync_secret_id_for(cal_id)
    _write_secret_text(sec_id, token)
    logger.info(f"Saved sync token for {cal_id[:8]}…")


async def send_telegram(text: str) -> None:
    if not text:
        return
    if len(text) > 4096:
        text = text[:4090] + "…"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text,
                # "parse_mode": "MarkdownV2",
            },
        )
        r.raise_for_status()


# ---------------- Calendar fetchers ----------------
def _initial_seed_sync_token(cal_id: str) -> None:
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
            nst = resp.get("nextSyncToken")
            if nst:
                _save_sync_token(cal_id, nst)
            return


def _delta_changes(cal_id: str, sync_token: str) -> Dict:
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
    summary = event.get("summary") or "No title"
    status = event.get("status")

    def fmt_time(value: str) -> str:
        if not value or value == "?":
            return "?"
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d %H:%M")
        except Exception:
            return value

    start = event.get("start", {})
    end = event.get("end", {})
    start_val = fmt_time(start.get("dateTime") or start.get("date") or "?")
    end_val = fmt_time(end.get("dateTime") or end.get("date") or "?")

    loc = event.get("location") or "—"
    desc = event.get("description") or "—"

    # Special case for cancelled events
    if status == "cancelled":
        lines = [
            f"❌ Event cancelled: {summary}",
            f"🕑 {start_val} → {end_val}",
            f"📍 {loc}",
            "",  # blank line
            f"📂 {label}",
        ]
        return "\n".join(lines)

    # Normal events
    summary_line = f"📅 {summary}"
    if status:
        summary_line += f" ({status.upper()})"

    lines = [
        summary_line,
        f"🕑 {start_val} → {end_val}",
        f"📍 {loc}",
        f"📝 {desc}",
        "",  # blank line
        f"📂 {label}",
    ]

    return "\n".join(lines)


# ---------------- Routes ----------------
@app.get("/health")
def root():
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request):
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")
    logger.info(f"Webhook: state={resource_state}, channel={channel_id}")

    if not channel_id or not resource_state:
        raise HTTPException(status_code=400, detail="Missing X-Goog- headers")

    mapped = _lookup_channel(channel_id)
    if not mapped:
        logger.warning(f"No channel mapping found for {channel_id} (register again?)")
        return {"status": "ok", "msg": "unknown channel"}

    cal_id, label = mapped

    if resource_state == "sync":
        return {"status": "ok", "msg": "sync handshake ignored"}

    token = _get_sync_token(cal_id)
    if not token:
        logger.info(f"No sync token for {label}. Seeding without notifications…")
        _initial_seed_sync_token(cal_id)
        return {"status": "ok", "msg": "seeded sync token"}

    try:
        result = _delta_changes(cal_id, token)
    except Exception as e:
        msg = str(e)
        if "410" in msg or "syncToken" in msg.lower():
            logger.warning(f"Sync token invalid for {label}. Re-seeding…")
            _initial_seed_sync_token(cal_id)
            return {"status": "ok", "msg": "reseeded"}
        logger.error(f"Error fetching deltas for {label}: {e}")
        return {"status": "error", "error": str(e)}

    if result.get("nextSyncToken"):
        _save_sync_token(cal_id, result["nextSyncToken"])

    sent = 0
    for ev in result.get("items", []):
        msg = _format_event_message(ev, label) or ""
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
        fake_event = {
            "summary": "!!!TEST!!!! Anna_Smith",
            "status": "confirmed",
            "start": {"dateTime": "2025-10-01T14:30:00+02:00"},
            "end": {"dateTime": "2025-10-01T16:00:00+02:00"},
            "location": "Diestsestraat 174, 3000 Leuven",
            "description": "Customer: Anna Smith\nService: Gel Nails + Hair Styling!",
        }
        label = "Rubina Calendar"

        msg = _format_event_message(fake_event, label) or ""
        logger.info(f"Test Telegram message:\n{msg}")

        await send_telegram(msg)
        return {"status": "ok", "message": msg}
    except Exception as e:
        logger.error(f"Test telegram failed: {e}")
        return {"status": "error", "error": str(e)}


# --- Secret helpers (adjusted mapping) ---
def _upsert_channel_mapping(
    channel_id: str, resource_id: str, cal_id: str, label: str
) -> None:
    """Store/refresh channel mapping with resourceId (needed for cleanup)."""
    current = _read_secret_text(CHANNEL_MAP_SECRET_ID) or ""
    lines = [ln for ln in current.splitlines() if ln.strip()]
    # remove any existing mapping for this channel
    lines = [ln for ln in lines if not ln.startswith(f"{channel_id}|")]
    lines.append(f"{channel_id}|{resource_id}|{cal_id}|{label}")
    _write_secret_text(CHANNEL_MAP_SECRET_ID, "\n".join(lines))
    logger.info(f"Saved channel mapping: {channel_id}|{resource_id}|{cal_id}|{label}")


def _lookup_channel(channel_id: str) -> Optional[Tuple[str, str]]:
    """Return (cal_id, label) for a channel_id, or None if not mapped."""
    data = _read_secret_text(CHANNEL_MAP_SECRET_ID)
    if not data:
        return None
    for ln in data.splitlines():
        parts = ln.strip().split("|", 3)
        if len(parts) == 4 and parts[0] == channel_id:
            return parts[2], parts[3]  # cal_id, label
    return None


# --- Register route ---
@app.get("/register")
def register_watch():
    results = []
    errors = []
    for cal_id, label in CALENDARS.items():
        try:
            body = {"id": str(uuid.uuid4()), "type": "web_hook", "address": WEBHOOK_URL}
            watch = (
                calendar_service.events().watch(calendarId=cal_id, body=body).execute()
            )
            channel_id = watch.get("id")
            resource_id = watch.get("resourceId")
            _upsert_channel_mapping(channel_id, resource_id, cal_id, label)
            results.append({"label": label, "watch": watch})
            logger.info(f"Registered watch for {label}")
        except Exception as e:
            msg = f"Calendar not found or not shared: {label} ({cal_id}). {e}"
            logger.error(msg)
            errors.append({"label": label, "error": msg})
    return {"channels": results, "errors": errors or None}


# --- Cleanup route ---
@app.get("/cleanup")
def cleanup_channels():
    data = _read_secret_text(CHANNEL_MAP_SECRET_ID)
    if not data:
        return {"status": "ok", "msg": "no channels to clean"}

    errors = []
    for ln in data.splitlines():
        try:
            channel_id, resource_id, cal_id, label = ln.split("|", 3)
        except ValueError:
            continue
        try:
            body = {"id": channel_id, "resourceId": resource_id}
            calendar_service.channels().stop(body=body).execute()
            logger.info(f"Stopped channel {channel_id} ({label})")
        except Exception as e:
            msg = f"Failed to stop {channel_id} ({label}): {e}"
            logger.error(msg)
            errors.append(msg)

    _write_secret_text(CHANNEL_MAP_SECRET_ID, "")
    return {"status": "ok", "errors": errors or None}
