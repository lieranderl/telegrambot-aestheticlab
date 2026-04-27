import importlib
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.app import create_app
from src.config import Settings
from src.dependencies import AppServices, get_services
from src.routes.admin import router as admin_router
from src.routes.health import router as health_router
from src.routes.webhook import router as webhook_router


class FakeWebhookService:
    def __init__(self, response=None) -> None:
        self.response = response or {"status": "ok"}
        self.calls = []

    async def handle_webhook(self, channel_id, resource_state):
        self.calls.append((channel_id, resource_state))
        return self.response


class FakeRegistrationService:
    def __init__(self) -> None:
        self.register_response = {"channels": [], "errors": None}
        self.cleanup_response = {"status": "ok", "errors": None}
        self.reset_response = {"status": "ok", "msg": "reset"}

    def register_all(self):
        return self.register_response

    def cleanup_all(self):
        return self.cleanup_response

    def reset_secret(self):
        return self.reset_response


class FakeTelegram:
    def __init__(self, should_fail: bool = False) -> None:
        self.should_fail = should_fail
        self.messages = []

    async def send_message(self, text):
        if self.should_fail:
            raise RuntimeError("boom")
        self.messages.append(text)


def build_route_app(services: AppServices) -> FastAPI:
    app = FastAPI()
    app.include_router(health_router)
    app.include_router(webhook_router)
    app.include_router(admin_router)
    app.dependency_overrides[get_services] = lambda: services
    return app


class DependenciesAndRoutesTests(unittest.TestCase):
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
            settings=None,
            telegram=FakeTelegram(),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_route_app(services)) as client:
            response = client.get("/health")

        self.assertEqual(response.json(), {"status": "ok"})

    def test_webhook_requires_headers(self) -> None:
        services = AppServices(
            settings=None,
            telegram=FakeTelegram(),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_route_app(services)) as client:
            response = client.post("/webhook")

        self.assertEqual(response.status_code, 400)

    def test_webhook_route_delegates(self) -> None:
        webhook_service = FakeWebhookService({"status": "ok", "sent": 2})
        services = AppServices(
            settings=None,
            telegram=FakeTelegram(),
            webhook_service=webhook_service,
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_route_app(services)) as client:
            response = client.post(
                "/webhook",
                headers={
                    "X-Goog-Channel-ID": "channel",
                    "X-Goog-Resource-State": "exists",
                },
            )

        self.assertEqual(response.json(), {"status": "ok", "sent": 2})
        self.assertEqual(webhook_service.calls, [("channel", "exists")])

    def test_admin_routes_and_test_telegram(self) -> None:
        registration_service = FakeRegistrationService()
        telegram = FakeTelegram()
        services = AppServices(
            settings=None,
            telegram=telegram,
            webhook_service=FakeWebhookService(),
            registration_service=registration_service,
        )
        with TestClient(build_route_app(services)) as client:
            self.assertEqual(
                client.get("/register").json(), registration_service.register_response
            )
            self.assertEqual(
                client.get("/cleanup").json(), registration_service.cleanup_response
            )
            self.assertEqual(
                client.get("/reset-secret").json(), registration_service.reset_response
            )
            test_response = client.get("/test-telegram")

        self.assertEqual(test_response.json()["status"], "ok")
        self.assertEqual(len(telegram.messages), 1)

    def test_test_telegram_handles_error(self) -> None:
        services = AppServices(
            settings=None,
            telegram=FakeTelegram(should_fail=True),
            webhook_service=FakeWebhookService(),
            registration_service=FakeRegistrationService(),
        )
        with TestClient(build_route_app(services)) as client:
            response = client.get("/test-telegram")

        self.assertEqual(response.json()["status"], "error")


class DummyCredentials:
    def with_scopes(self, scopes):
        return self


class DummyAsyncClient:
    def __init__(self, timeout):
        self.timeout = timeout
        self.closed = False

    async def aclose(self):
        self.closed = True


class FakeSecretStore:
    channel_map_secret_id = "calendar-channel-map"
    ensure_error = None
    last_instance = None

    def __init__(self, client, project_id):
        self.client = client
        self.project_id = project_id
        self.ensure_calls = []
        self.reset_calls = 0
        FakeSecretStore.last_instance = self

    def ensure_secret(self, secret_id):
        self.ensure_calls.append(secret_id)
        if self.ensure_error:
            raise self.ensure_error

    def reset_channel_mapping_secret(self):
        self.reset_calls += 1


class FakeGateway:
    def __init__(self, *args):
        self.args = args


class AppWiringTests(unittest.TestCase):
    def setUp(self) -> None:
        FakeSecretStore.ensure_error = None
        FakeSecretStore.last_instance = None

    def _settings(self, project_id="project-a"):
        return Settings(
            telegram_token="token",
            telegram_chat_id="chat",
            webhook_url="https://example.com/webhook",
            raw_calendars="one@example.com|One",
            google_cloud_project=project_id,
            gcp_project=None,
        )

    def test_create_app_wires_services_and_closes_client(self) -> None:
        dummy_client = DummyAsyncClient(timeout=20.0)
        with (
            patch("src.app.Settings.from_env", return_value=self._settings()),
            patch(
                "src.app.google_auth_default",
                return_value=(DummyCredentials(), "project-from-adc"),
            ),
            patch("src.app.build", return_value="calendar-service"),
            patch(
                "src.app.secretmanager_v1.SecretManagerServiceClient",
                return_value="secret-client",
            ),
            patch("src.app.httpx.AsyncClient", return_value=dummy_client),
            patch("src.app.SecretStore", FakeSecretStore),
            patch("src.app.CalendarGateway", FakeGateway),
            patch("src.app.TelegramGateway", FakeGateway),
            patch("src.app.WebhookService", FakeGateway),
            patch("src.app.RegistrationService", FakeGateway),
        ):
            with TestClient(create_app()) as client:
                response = client.get("/health")

        self.assertEqual(response.json(), {"status": "ok"})
        self.assertEqual(
            FakeSecretStore.last_instance.ensure_calls, ["calendar-channel-map"]
        )
        self.assertTrue(dummy_client.closed)

    def test_create_app_uses_settings_project_when_adc_missing(self) -> None:
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
            patch(
                "src.app.secretmanager_v1.SecretManagerServiceClient",
                return_value="secret-client",
            ),
            patch("src.app.httpx.AsyncClient", return_value=dummy_client),
            patch("src.app.SecretStore", FakeSecretStore),
            patch("src.app.CalendarGateway", FakeGateway),
            patch("src.app.TelegramGateway", FakeGateway),
            patch("src.app.WebhookService", FakeGateway),
            patch("src.app.RegistrationService", FakeGateway),
        ):
            with TestClient(create_app()) as client:
                client.get("/health")

        self.assertEqual(FakeSecretStore.last_instance.project_id, "settings-project")

    def test_create_app_raises_when_project_id_missing(self) -> None:
        settings = Settings(
            telegram_token="token",
            telegram_chat_id="chat",
            webhook_url="https://example.com/webhook",
            raw_calendars="one@example.com|One",
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
                with TestClient(create_app()):
                    pass

    def test_create_app_resets_secret_when_ensure_fails(self) -> None:
        FakeSecretStore.ensure_error = RuntimeError("boom")
        dummy_client = DummyAsyncClient(timeout=20.0)
        with (
            patch("src.app.Settings.from_env", return_value=self._settings()),
            patch(
                "src.app.google_auth_default",
                return_value=(DummyCredentials(), "project"),
            ),
            patch("src.app.build", return_value="calendar-service"),
            patch(
                "src.app.secretmanager_v1.SecretManagerServiceClient",
                return_value="secret-client",
            ),
            patch("src.app.httpx.AsyncClient", return_value=dummy_client),
            patch("src.app.SecretStore", FakeSecretStore),
            patch("src.app.CalendarGateway", FakeGateway),
            patch("src.app.TelegramGateway", FakeGateway),
            patch("src.app.WebhookService", FakeGateway),
            patch("src.app.RegistrationService", FakeGateway),
        ):
            with TestClient(create_app()) as client:
                client.get("/health")

        self.assertEqual(FakeSecretStore.last_instance.reset_calls, 1)

    def test_main_module_creates_app_on_import(self) -> None:
        sentinel = object()
        sys.modules.pop("src.main", None)
        with patch("src.app.create_app", return_value=sentinel):
            module = importlib.import_module("src.main")

        self.assertIs(module.app, sentinel)
        sys.modules.pop("src.main", None)


if __name__ == "__main__":
    unittest.main()
