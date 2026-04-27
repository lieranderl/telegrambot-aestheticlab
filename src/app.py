import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from google.auth import default as google_auth_default
from googleapiclient.discovery import build

from .config import Settings
from .dependencies import AppServices
from .gateways.calendar_api import CalendarGateway
from .gateways.firestore_state_store import FirestoreStateStore
from .gateways.telegram_api import TelegramGateway
from .routes.admin import router as admin_router
from .routes.health import router as health_router
from .routes.webhook import router as webhook_router
from .services.registration import RegistrationService
from .services.webhook_service import WebhookService

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

CALENDAR_SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]
STATE_STORE_SCOPES = ["https://www.googleapis.com/auth/datastore"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings.from_env()
    logger.info("Configured calendars: %s", settings.calendar_labels)

    base_credentials, detected_project_id = google_auth_default()
    project_id = detected_project_id or settings.project_id
    if not project_id:
        raise RuntimeError(
            "Unable to determine Google Cloud project. Set GOOGLE_CLOUD_PROJECT or GCP_PROJECT."
        )

    logger.info("Using project: %s", project_id)
    calendar_credentials = (
        base_credentials.with_scopes(CALENDAR_SCOPES)
        if hasattr(base_credentials, "with_scopes")
        else base_credentials
    )
    state_store_credentials = (
        base_credentials.with_scopes(STATE_STORE_SCOPES)
        if hasattr(base_credentials, "with_scopes")
        else base_credentials
    )

    calendar_gateway = CalendarGateway(
        build(
            "calendar",
            "v3",
            credentials=calendar_credentials,
            cache_discovery=False,
        )
    )
    http_client = httpx.AsyncClient(timeout=20.0)
    state_store = FirestoreStateStore(
        http_client,
        state_store_credentials,
        project_id,
        settings.state_collection_prefix,
    )
    telegram_gateway = TelegramGateway(
        settings.telegram_token,
        settings.telegram_chat_id,
        http_client,
    )

    app.state.services = AppServices(
        settings=settings,
        telegram=telegram_gateway,
        webhook_service=WebhookService(
            calendar_gateway,
            state_store,
            telegram_gateway,
        ),
        registration_service=RegistrationService(
            calendar_gateway,
            state_store,
            settings.calendars,
            settings.webhook_url,
        ),
    )

    try:
        yield
    finally:
        await http_client.aclose()


def create_public_app() -> FastAPI:
    app = FastAPI(title="Calendar→Telegram webhook", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(webhook_router)
    return app


def create_admin_app() -> FastAPI:
    app = FastAPI(title="Calendar admin", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(admin_router)
    return app


create_app = create_public_app
