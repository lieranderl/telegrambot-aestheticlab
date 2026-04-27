from fastapi import APIRouter, Depends, HTTPException, Request

from ..dependencies import AppServices, get_services

router = APIRouter()


@router.post("/webhook")
async def webhook(
    request: Request,
    services: AppServices = Depends(get_services),
) -> dict[str, object]:
    channel_id = request.headers.get("X-Goog-Channel-ID")
    resource_state = request.headers.get("X-Goog-Resource-State")

    if not channel_id or not resource_state:
        raise HTTPException(status_code=400, detail="Missing X-Goog- headers")

    return await services.webhook_service.handle_webhook(channel_id, resource_state)
