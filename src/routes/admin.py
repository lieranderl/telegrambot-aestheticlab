import logging

from fastapi import APIRouter, Depends, HTTPException

from ..dependencies import AppServices, get_services
from ..services.formatting import format_event_message

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin")


@router.post("/register")
async def register_watch(
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.register_all()


@router.post("/cleanup")
async def cleanup_channels(
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.cleanup_all()


@router.post("/renew")
async def renew_channels(
    within_minutes: int | None = None,
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return await services.registration_service.renew_expiring_channels(
        within_minutes or services.settings.renewal_lead_minutes
    )


@router.post("/test-telegram")
async def test_telegram(
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
        logger.error("event=admin_test_telegram_failed error=%s", exc)
        raise HTTPException(status_code=502, detail="Telegram test failed") from exc
