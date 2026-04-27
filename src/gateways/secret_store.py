import logging

from google.api_core.exceptions import AlreadyExists, NotFound
from google.cloud import secretmanager_v1

from ..models import ChannelMapping
from ..utils.ids import sync_secret_id_for

logger = logging.getLogger(__name__)


class SecretStore:
    channel_map_secret_id = "calendar-channel-map"

    def __init__(
        self,
        client: secretmanager_v1.SecretManagerServiceClient,
        project_id: str,
    ) -> None:
        self._client = client
        self._project_id = project_id

    def _secret_full_name(self, secret_id: str) -> str:
        return f"projects/{self._project_id}/secrets/{secret_id}"

    def ensure_secret(self, secret_id: str) -> None:
        parent = f"projects/{self._project_id}"
        try:
            self._client.create_secret(
                parent=parent,
                secret_id=secret_id,
                secret=secretmanager_v1.Secret(
                    replication=secretmanager_v1.Replication(
                        automatic=secretmanager_v1.Replication.Automatic()
                    )
                ),
            )
            logger.info("Created secret: %s", secret_id)
        except AlreadyExists:
            return

    def read_text(self, secret_id: str) -> str | None:
        parent = self._secret_full_name(secret_id)
        try:
            response = self._client.access_secret_version(
                name=f"{parent}/versions/latest"
            )
            return response.payload.data.decode("utf-8")
        except Exception as exc:
            if "DESTROYED" in str(exc) or "FailedPrecondition" in str(exc):
                try:
                    versions = list(
                        self._client.list_secret_versions(request={"parent": parent})
                    )
                    active_versions = [
                        version
                        for version in versions
                        if hasattr(version, "state") and version.state.name == "ENABLED"
                    ]
                    if not active_versions:
                        logger.warning(
                            "No active versions found for secret %s", secret_id
                        )
                        return None

                    active_versions.sort(
                        key=lambda version: (
                            version.create_time.seconds
                            + version.create_time.nanos / 1e9
                        ),
                        reverse=True,
                    )
                    response = self._client.access_secret_version(
                        name=active_versions[0].name
                    )
                    return response.payload.data.decode("utf-8")
                except Exception as inner_exc:
                    logger.warning(
                        "Failed to find active version for %s: %s",
                        secret_id,
                        inner_exc,
                    )
                    return None

            if isinstance(exc, NotFound):
                return None

            logger.warning("Failed to read secret %s: %s", secret_id, exc)
            return None

    def write_text(self, secret_id: str, text: str) -> None:
        self.ensure_secret(secret_id)
        parent = self._secret_full_name(secret_id)
        payload_text = text.strip() or "{}"
        new_version = self._client.add_secret_version(
            parent=parent,
            payload=secretmanager_v1.SecretPayload(data=payload_text.encode("utf-8")),
        )

        versions = list(self._client.list_secret_versions(request={"parent": parent}))
        for version in versions:
            if version.name == new_version.name:
                continue

            try:
                state = version.state.name if hasattr(version, "state") else None
                if state and state.upper() == "DESTROYED":
                    continue
                if state and state.upper() == "ENABLED":
                    try:
                        self._client.disable_secret_version(name=version.name)
                    except Exception as disable_exc:
                        logger.warning(
                            "Failed to disable %s: %s", version.name, disable_exc
                        )
                self._client.destroy_secret_version(name=version.name)
            except Exception as exc:
                logger.warning("Failed to clean up %s: %s", version.name, exc)

    def get_sync_token(self, calendar_id: str) -> str | None:
        return self.read_text(sync_secret_id_for(calendar_id))

    def save_sync_token(self, calendar_id: str, token: str) -> None:
        self.write_text(sync_secret_id_for(calendar_id), token)

    def load_channel_mappings(self) -> list[ChannelMapping]:
        data = self.read_text(self.channel_map_secret_id)
        if not data:
            return []

        mappings: list[ChannelMapping] = []
        for line in data.splitlines():
            mapping = ChannelMapping.from_line(line)
            if mapping:
                mappings.append(mapping)
        return mappings

    def save_channel_mappings(self, mappings: list[ChannelMapping]) -> None:
        self.write_text(
            self.channel_map_secret_id,
            "\n".join(mapping.to_line() for mapping in mappings),
        )

    def upsert_channel_mapping(
        self,
        channel_id: str,
        resource_id: str,
        calendar_id: str,
        label: str,
    ) -> None:
        mappings = [
            mapping
            for mapping in self.load_channel_mappings()
            if mapping.channel_id != channel_id
        ]
        mappings.append(
            ChannelMapping(
                channel_id=channel_id,
                resource_id=resource_id,
                calendar_id=calendar_id,
                label=label,
            )
        )
        self.save_channel_mappings(mappings)

    def lookup_channel(self, channel_id: str) -> ChannelMapping | None:
        for mapping in self.load_channel_mappings():
            if mapping.channel_id == channel_id:
                return mapping
        return None

    def reset_channel_mapping_secret(self) -> None:
        secret_name = self._secret_full_name(self.channel_map_secret_id)
        try:
            self._client.delete_secret(name=secret_name)
            logger.info("Deleted existing secret: %s", self.channel_map_secret_id)
        except Exception as exc:
            logger.info(
                "Secret %s doesn't exist or couldn't be deleted: %s",
                self.channel_map_secret_id,
                exc,
            )

        self.ensure_secret(self.channel_map_secret_id)
