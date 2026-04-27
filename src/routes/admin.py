import logging

from fastapi import APIRouter, Depends, Header, HTTPException

from ..dependencies import AppServices, get_services
from ..services.formatting import format_event_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")


def require_admin_token(
    x_admin_token: str | None = Header(default=None),
    services: AppServices = Depends(get_services),
) -> None:
    expected = services.settings.admin_api_token
    if not expected or x_admin_token != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/register")
async def register_watch(
    _: None = Depends(require_admin_token),
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.register_all()


@router.post("/cleanup")
async def cleanup_channels(
    _: None = Depends(require_admin_token),
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.cleanup_all()


@router.post("/renew")
async def renew_channels(
    within_minutes: int | None = None,
    _: None = Depends(require_admin_token),
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.renew_expiring_channels(
        within_minutes or services.settings.renewal_lead_minutes
    )


@router.post("/test-telegram")
async def test_telegram(
    _: None = Depends(require_admin_token),
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    try:
        fake_event = {
            "summary": "!!!TEST!!!! Anna_Smith",
            "status": "confirmed",
            "start": {"dateTime": "2025-10-01T14:30:00+02:00"},
            "end": {"dateTime": "2025-10-01T16:00:00+02:00"},
            "location": "Diestsestraat 174, 3000 Leuven",
            "description": "Customer: Anna Smith\nService: Gel Nails + Hair Styling!",
        }
        message = format_event_message(fake_event, "Rubina Calendar") or ""
        logger.info("Test Telegram message:\n%s", message)
        await services.telegram.send_message(message)
        return {"status": "ok", "message": message}
    except Exception as exc:
        logger.error("Test telegram failed: %s", exc)
        return {"status": "error", "error": str(exc)}
