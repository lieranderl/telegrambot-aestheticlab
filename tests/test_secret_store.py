import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.api_core.exceptions import AlreadyExists, NotFound

from src.gateways.secret_store import SecretStore
from src.models import ChannelMapping


def make_version(name: str, state: str, seconds: int = 1):
    return SimpleNamespace(
        name=name,
        state=SimpleNamespace(name=state),
        create_time=SimpleNamespace(seconds=seconds, nanos=0),
    )


def make_access_response(text: str):
    return SimpleNamespace(payload=SimpleNamespace(data=text.encode("utf-8")))


class FakeSecretClient:
    def __init__(self) -> None:
        self.create_secret_exc = None
        self.access_responses = {}
        self.version_lists = {}
        self.added_versions = []
        self.disable_calls = []
        self.destroy_calls = []
        self.delete_calls = []

    def create_secret(self, **kwargs):
        if self.create_secret_exc:
            raise self.create_secret_exc
        self.create_secret_kwargs = kwargs

    def access_secret_version(self, name: str):
        response = self.access_responses[name]
        if isinstance(response, Exception):
            raise response
        return response

    def list_secret_versions(self, request):
        return self.version_lists[request["parent"]]

    def add_secret_version(self, parent, payload):
        self.added_versions.append((parent, payload.data.decode("utf-8")))
        return SimpleNamespace(name=f"{parent}/versions/new")

    def disable_secret_version(self, name):
        self.disable_calls.append(name)

    def destroy_secret_version(self, name):
        self.destroy_calls.append(name)

    def delete_secret(self, name):
        self.delete_calls.append(name)


class MemorySecretStore(SecretStore):
    def __init__(self, client, project_id: str) -> None:
        super().__init__(client, project_id)
        self.saved = {}

    def read_text(self, secret_id: str):
        return self.saved.get(secret_id)

    def write_text(self, secret_id: str, text: str) -> None:
        self.saved[secret_id] = text


class SecretStoreTests(unittest.TestCase):
    def test_ensure_secret_ignores_already_exists(self) -> None:
        client = FakeSecretClient()
        client.create_secret_exc = AlreadyExists("exists")
        store = SecretStore(client, "project")

        store.ensure_secret("secret-id")

    def test_read_text_returns_latest_version(self) -> None:
        client = FakeSecretClient()
        client.access_responses["projects/project/secrets/secret/versions/latest"] = (
            make_access_response("value")
        )
        store = SecretStore(client, "project")

        self.assertEqual(store.read_text("secret"), "value")

    def test_read_text_falls_back_to_latest_active_version(self) -> None:
        client = FakeSecretClient()
        parent = "projects/project/secrets/secret"
        client.access_responses[f"{parent}/versions/latest"] = RuntimeError("DESTROYED")
        client.version_lists[parent] = [
            make_version(f"{parent}/versions/1", "DESTROYED", 1),
            make_version(f"{parent}/versions/2", "ENABLED", 2),
        ]
        client.access_responses[f"{parent}/versions/2"] = make_access_response(
            "fallback"
        )
        store = SecretStore(client, "project")

        self.assertEqual(store.read_text("secret"), "fallback")

    def test_read_text_returns_none_for_missing_secret(self) -> None:
        client = FakeSecretClient()
        client.access_responses["projects/project/secrets/secret/versions/latest"] = (
            NotFound("missing")
        )
        store = SecretStore(client, "project")

        self.assertIsNone(store.read_text("secret"))

    def test_read_text_returns_none_when_no_active_versions(self) -> None:
        client = FakeSecretClient()
        parent = "projects/project/secrets/secret"
        client.access_responses[f"{parent}/versions/latest"] = RuntimeError("DESTROYED")
        client.version_lists[parent] = [
            make_version(f"{parent}/versions/1", "DESTROYED", 1)
        ]
        store = SecretStore(client, "project")

        self.assertIsNone(store.read_text("secret"))

    def test_read_text_returns_none_when_fallback_listing_fails(self) -> None:
        client = FakeSecretClient()
        parent = "projects/project/secrets/secret"
        client.access_responses[f"{parent}/versions/latest"] = RuntimeError(
            "FailedPrecondition"
        )
        client.version_lists[parent] = RuntimeError("boom")
        store = SecretStore(client, "project")

        with patch.object(
            client, "list_secret_versions", side_effect=RuntimeError("boom")
        ):
            self.assertIsNone(store.read_text("secret"))

    def test_read_text_returns_none_for_generic_error(self) -> None:
        client = FakeSecretClient()
        client.access_responses["projects/project/secrets/secret/versions/latest"] = (
            RuntimeError("boom")
        )
        store = SecretStore(client, "project")

        self.assertIsNone(store.read_text("secret"))

    def test_write_text_disables_and_destroys_old_versions(self) -> None:
        client = FakeSecretClient()
        parent = "projects/project/secrets/secret"
        client.version_lists[parent] = [
            make_version(f"{parent}/versions/new", "ENABLED", 2),
            make_version(f"{parent}/versions/old-enabled", "ENABLED", 1),
            make_version(f"{parent}/versions/old-destroyed", "DESTROYED", 0),
            make_version(f"{parent}/versions/old-disabled", "DISABLED", 0),
        ]
        store = SecretStore(client, "project")

        with patch.object(store, "ensure_secret") as ensure_secret:
            store.write_text("secret", " value ")

        ensure_secret.assert_called_once_with("secret")
        self.assertEqual(client.added_versions[0], (parent, "value"))
        self.assertIn(f"{parent}/versions/old-enabled", client.disable_calls)
        self.assertIn(f"{parent}/versions/old-enabled", client.destroy_calls)
        self.assertIn(f"{parent}/versions/old-disabled", client.destroy_calls)
        self.assertNotIn(f"{parent}/versions/old-destroyed", client.destroy_calls)

    def test_get_and_save_sync_token_use_hashed_secret_names(self) -> None:
        store = MemorySecretStore(FakeSecretClient(), "project")
        store.save_sync_token("calendar@example.com", "token")

        self.assertEqual(store.get_sync_token("calendar@example.com"), "token")

    def test_mapping_helpers_round_trip(self) -> None:
        store = MemorySecretStore(FakeSecretClient(), "project")
        store.upsert_channel_mapping("channel", "resource", "calendar", "Main")
        store.upsert_channel_mapping("channel", "resource-2", "calendar", "Main")

        mappings = store.load_channel_mappings()

        self.assertEqual(len(mappings), 1)
        self.assertEqual(mappings[0].resource_id, "resource-2")
        self.assertEqual(store.lookup_channel("channel"), mappings[0])
        self.assertIsNone(store.lookup_channel("missing"))

    def test_reset_channel_mapping_secret_deletes_and_recreates(self) -> None:
        client = FakeSecretClient()
        store = SecretStore(client, "project")

        with patch.object(store, "ensure_secret") as ensure_secret:
            store.reset_channel_mapping_secret()

        self.assertEqual(
            client.delete_calls, ["projects/project/secrets/calendar-channel-map"]
        )
        ensure_secret.assert_called_once_with("calendar-channel-map")

    def test_reset_channel_mapping_secret_tolerates_delete_failure(self) -> None:
        client = FakeSecretClient()
        store = SecretStore(client, "project")

        with patch.object(client, "delete_secret", side_effect=RuntimeError("boom")):
            with patch.object(store, "ensure_secret") as ensure_secret:
                store.reset_channel_mapping_secret()

        ensure_secret.assert_called_once_with("calendar-channel-map")


if __name__ == "__main__":
    unittest.main()
