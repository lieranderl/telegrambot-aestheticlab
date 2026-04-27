from dataclasses import dataclass

from fastapi import Request

from .config import Settings
from .gateways.telegram_api import TelegramGateway
from .services.registration import RegistrationService
from .services.webhook_service import WebhookService


@dataclass
class AppServices:
    settings: Settings
    telegram: TelegramGateway
    webhook_service: WebhookService
    registration_service: RegistrationService


def get_services(request: Request) -> AppServices:
    return request.app.state.services
