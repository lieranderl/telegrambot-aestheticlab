import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app import create_admin_app, create_public_app
from src.config import Settings
from src.dependencies import AppServices, get_services
from src.errors import (
    StateStoreUnavailableError,
    WebhookAuthenticationError,
    WebhookProcessingError,
)
from src.routes.admin import router as admin_router
from src.routes.health import router as health_router
from src.routes.webhook import router as webhook_router


class FakeWebhookService:
    def __init__(self, response=None, error=None) -> None:
        self.response = response or {"status": "ok"}
        self.error = error
        self.calls = []

    async def handle_webhook(
        self, channel_id, channel_token, resource_id, resource_state
    ):
        self.calls.append((channel_id, channel_token, resource_id, resource_state))
        if self.error:
            raise self.error
        return self.response


class FakeRegistrationService:
    def __init__(self) -> None:
        self.register_response = {"channels": [], "errors": None}
        self.cleanup_response = {"status": "ok", "errors": None}
        self.renew_response = {"status": "ok", "renewed": [], "errors": None}

    async def register_all(self):
        return self.register_response

    async def cleanup_all(self):
        return self.cleanup_response

    async def renew_expiring_channels(self, within_minutes):
        self.last_renewal = within_minutes
        return self.renew_response


class FakeTelegram:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.messages = []

    async def send_message(self, text):
        if self.should_fail:
            raise RuntimeError("boom")
        self.messages.append(text)


def build_public_route_app(services: AppServices) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.dependency_overrides[get_services] = lambda: services
    return app


def build_admin_route_app(services: AppServices) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_services] = lambda: services
    return app


class DependenciesAndRoutesTests(unittest.TestCase):
    def _settings(self):
        return Settings(
            telegram_token="token",
            telegram_chat_id="chat",
            webhook_url="https://example.com/webhook",
            raw_calendars="one@example.com|One",
            state_collection_prefix="prefix",
            renewal_lead_minutes=120,
            google_cloud_project="project-a",
            gcp_project=None,
        )

    def test_get_services_returns_app_state(self) -> None:
        services = AppServices(
            settings=None,
            telegram=None,
            webhook_service=None,
            registration_service=None,
        )
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(services=services))
        )

        self.assertIs(get_services(request), services)

    def test_health_route(self) -> None:
        services = AppServices(
            settings=self._settings(),
            telegram=FakeTelegram(),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_public_route_app(services)) as client:
            response = client.get("/health")

        self.assertEqual(response.json(), {"status": "ok"})

    def test_webhook_requires_headers(self) -> None:
        services = AppServices(
            settings=self._settings(),
            telegram=FakeTelegram(),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_public_route_app(services)) as client:
            response = client.post("/webhook")

        self.assertEqual(response.status_code, 400)

    def test_webhook_route_delegates(self) -> None:
        webhook_service = FakeWebhookService({"status": "ok", "sent": 2})
        services = AppServices(
            settings=self._settings(),
            telegram=FakeTelegram(),
            webhook_service=webhook_service,
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_public_route_app(services)) as client:
            response = client.post(
                "/webhook",
                headers={
                    "X-Goog-Channel-ID": "channel",
                    "X-Goog-Channel-Token": "token-1",
                    "X-Goog-Resource-ID": "resource-1",
                    "X-Goog-Resource-State": "exists",
                },
            )

        self.assertEqual(response.json(), {"status": "ok", "sent": 2})
        self.assertEqual(
            webhook_service.calls,
            [("channel", "token-1", "resource-1", "exists")],
        )

    def test_webhook_route_maps_errors(self) -> None:
        scenarios = [
            (WebhookAuthenticationError("bad"), 403),
            (StateStoreUnavailableError("down"), 503),
            (WebhookProcessingError("retry"), 503),
        ]
        for error, expected_code in scenarios:
            services = AppServices(
                settings=self._settings(),
                telegram=FakeTelegram(),
                webhook_service=FakeWebhookService(error=error),
                registration_service=FakeRegistrationService(),
            )
            with TestClient(build_public_route_app(services)) as client:
                response = client.post(
                    "/webhook",
                    headers={
                        "X-Goog-Channel-ID": "channel",
                        "X-Goog-Channel-Token": "token-1",
                        "X-Goog-Resource-ID": "resource-1",
                        "X-Goog-Resource-State": "exists",
                    },
                )
            self.assertEqual(response.status_code, expected_code)

    def test_admin_routes_and_test_telegram(self) -> None:
        registration_service = FakeRegistrationService()
        telegram = FakeTelegram()
        services = AppServices(
            settings=self._settings(),
            telegram=telegram,
            webhook_service=FakeWebhookService(),
            registration_service=registration_service,
        )
        with TestClient(build_admin_route_app(services)) as client:
            self.assertEqual(
                client.post("/admin/register").json(),
                registration_service.register_response,
            )
            self.assertEqual(
                client.post("/admin/cleanup").json(),
                registration_service.cleanup_response,
            )
            self.assertEqual(
                client.post("/admin/renew").json(),
                registration_service.renew_response,
            )
            test_response = client.post("/admin/test-telegram")

        self.assertEqual(test_response.json()["status"], "ok")
        self.assertEqual(len(telegram.messages), 1)

    def test_test_telegram_handles_error(self) -> None:
        services = AppServices(
            settings=self._settings(),
            telegram=FakeTelegram(should_fail=True),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_admin_route_app(services)) as client:
            response = client.post("/admin/test-telegram")

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "Telegram test failed")


class DummyCredentials:
    def with_scopes(self, scopes):
        return self


class DummyAsyncClient:
    def __init__(self, timeout):
        self.timeout = timeout
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeStateStore:
    last_instance = None

    def __init__(
        self, client, credentials, project_id, collection_prefix, delivery_ttl_days
    ):
        self.client = client
        self.credentials = credentials
        self.project_id = project_id
        self.collection_prefix = collection_prefix
        self.delivery_ttl_days = delivery_ttl_days
        FakeStateStore.last_instance = self


class FakeGateway:
    def __init__(self, *args):
        self.args = args


class AppWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeStateStore.last_instance = None

    def _settings(self, project_id="project-a"):
        return Settings(
            telegram_token="token",
            telegram_chat_id="chat",
            webhook_url="https://example.com/webhook",
            raw_calendars="one@example.com|One",
            state_collection_prefix="prefix",
            renewal_lead_minutes=120,
            google_cloud_project=project_id,
            gcp_project=None,
        )

    def test_create_public_app_wires_services_and_closes_client(self) -> None:
        dummy_client = DummyAsyncClient(timeout=20.0)
        with (
            patch("src.app.Settings.from_env", return_value=self._settings()),
            patch(
                "src.app.google_auth_default",
                return_value=(DummyCredentials(), "project-from-adc"),
            ),
            patch("src.app.build", return_value="calendar-service"),
            patch("src.app.httpx.AsyncClient", return_value=dummy_client),
            patch("src.app.FirestoreStateStore", FakeStateStore),
            patch("src.app.CalendarGateway", FakeGateway),
            patch("src.app.TelegramGateway", FakeGateway),
            patch("src.app.WebhookService", FakeGateway),
            patch("src.app.RegistrationService", FakeGateway),
        ):
            with TestClient(create_public_app()) as client:
                response = client.get("/health")

        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(FakeStateStore.last_instance.project_id, "project-from-adc")
        self.assertEqual(FakeStateStore.last_instance.delivery_ttl_days, 30)
        self.assertTrue(dummy_client.closed)

    def test_create_admin_app_uses_settings_project_when_adc_missing(self) -> None:
        dummy_client = DummyAsyncClient(timeout=20.0)
        with (
            patch(
                "src.app.Settings.from_env",
                return_value=self._settings(project_id="settings-project"),
            ),
            patch(
                "src.app.google_auth_default", return_value=(DummyCredentials(), None)
            ),
            patch("src.app.build", return_value="calendar-service"),
            patch("src.app.httpx.AsyncClient", return_value=dummy_client),
            patch("src.app.FirestoreStateStore", FakeStateStore),
            patch("src.app.CalendarGateway", FakeGateway),
            patch("src.app.TelegramGateway", FakeGateway),
            patch("src.app.WebhookService", FakeGateway),
            patch("src.app.RegistrationService", FakeGateway),
        ):
            with TestClient(create_admin_app()) as client:
                client.get("/health")

        self.assertEqual(FakeStateStore.last_instance.project_id, "settings-project")

    def test_create_app_raises_when_project_id_missing(self) -> None:
        settings = Settings(
            telegram_token="token",
            telegram_chat_id="chat",
            webhook_url="https://example.com/webhook",
            raw_calendars="one@example.com|One",
            state_collection_prefix="prefix",
            renewal_lead_minutes=120,
            google_cloud_project=None,
            gcp_project=None,
        )
        with (
            patch("src.app.Settings.from_env", return_value=settings),
            patch(
                "src.app.google_auth_default", return_value=(DummyCredentials(), None)
            ),
        ):
            with self.assertRaises(RuntimeError):
                with TestClient(create_public_app()):
                    pass

    def test_main_modules_create_apps_on_import(self) -> None:
        public_sentinel = object()
        admin_sentinel = object()
        sys.modules.pop("src.main", None)
        sys.modules.pop("src.admin_main", None)
        with (
            patch("src.app.create_public_app", return_value=public_sentinel),
            patch("src.app.create_admin_app", return_value=admin_sentinel),
        ):
            public_module = importlib.import_module("src.main")
            admin_module = importlib.import_module("src.admin_main")

        self.assertIs(public_module.app, public_sentinel)
        self.assertIs(admin_module.app, admin_sentinel)
        sys.modules.pop("src.main", None)
        sys.modules.pop("src.admin_main", None)


if __name__ == "__main__":
    unittest.main()
