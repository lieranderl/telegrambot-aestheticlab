from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import AppServices, get_services
from ..errors import (
    StateStoreUnavailableError,
    WebhookAuthenticationError,
    WebhookProcessingError,
)

router = APIRouter()


@router.post("/webhook")
async def webhook(
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    channel_id = request.headers.get("X-Goog-Channel-ID")
    channel_token = request.headers.get("X-Goog-Channel-Token")
    resource_id = request.headers.get("X-Goog-Resource-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")

    if not channel_id or not channel_token or not resource_id or not resource_state:
        raise HTTPException(status_code=400, detail="Missing X-Goog- headers")

    try:
        return await services.webhook_service.handle_webhook(
            channel_id,
            channel_token,
            resource_id,
            resource_state,
        )
    except WebhookAuthenticationError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except StateStoreUnavailableError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except WebhookProcessingError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
