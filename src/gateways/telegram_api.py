import httpx


class TelegramGateway:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client

    async def send_message(self, text: str) -> None:
        if not text:
            return

        if len(text) > 4096:
            text = text[:4090] + "…"

        response = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text},
        )
        response.raise_for_status()
