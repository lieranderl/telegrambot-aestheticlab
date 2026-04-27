import httpx
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential


_MAX_TELEGRAM_MESSAGE_LENGTH = 4096


def _split_message(text: str) -> list[str]:
    if len(text) <= _MAX_TELEGRAM_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > _MAX_TELEGRAM_MESSAGE_LENGTH:
            raise ValueError("Telegram message line exceeds 4096 characters")
        if len(current) + len(line) > _MAX_TELEGRAM_MESSAGE_LENGTH:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line

    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


def _is_retryable_telegram_error(exc: BaseException) -> bool:
    if isinstance(exc, httpx.TimeoutException):
        return True
    if not isinstance(exc, httpx.HTTPStatusError):
        return False
    status_code = exc.response.status_code
    return status_code in {408, 429} or status_code >= 500


class TelegramGateway:
    def __init__(self, token: str, chat_id: str, client: httpx.AsyncClient) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = client

    async def send_message(self, text: str) -> None:
        if not text:
            return

        for chunk in _split_message(text):
            await self._post_message(chunk)

    @retry(
        retry=retry_if_exception(_is_retryable_telegram_error),
        wait=wait_exponential(multiplier=1, min=1, max=8),
        stop=stop_after_attempt(3),
        reraise=True,
    )
    async def _post_message(self, text: str) -> None:
        response = await self._client.post(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            json={"chat_id": self._chat_id, "text": text, "parse_mode": "HTML"},
        )
        response.raise_for_status()
