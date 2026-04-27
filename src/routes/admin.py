import logging

from fastapi import APIRouter, Depends

from ..dependencies import AppServices, get_services
from ..services.formatting import format_event_message

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/register")
def register_watch(
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return services.registration_service.register_all()


@router.get("/cleanup")
def cleanup_channels(
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return services.registration_service.cleanup_all()


@router.get("/reset-secret")
def reset_secret(
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    return services.registration_service.reset_secret()


@router.get("/test-telegram")
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
        logger.error("Test telegram failed: %s", exc)
        return {"status": "error", "error": str(exc)}
