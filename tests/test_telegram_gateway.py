import unittest

import httpx

from src.gateways.telegram_api import (
    TelegramGateway,
    _is_retryable_telegram_error,
    _split_message,
)


class FakeResponse:
    def __init__(self) -> None:
        self.status_checked = False

    def raise_for_status(self) -> None:
        self.status_checked = True


class FakeAsyncClient:
    def __init__(self) -> None:
        self.posts = []
        self.response = FakeResponse()

    async def post(self, url, json):
        self.posts.append((url, json))
        return self.response


class TelegramGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_skips_empty_text(self) -> None:
        client = FakeAsyncClient()
        gateway = TelegramGateway("token", "chat", client)

        await gateway.send_message("")

        self.assertEqual(client.posts, [])

    async def test_send_message_posts(self) -> None:
        client = FakeAsyncClient()
        gateway = TelegramGateway("token", "chat", client)

        await gateway.send_message("hello")

        url, payload = client.posts[0]
        self.assertIn("/bottoken/sendMessage", url)
        self.assertEqual(payload["chat_id"], "chat")
        self.assertEqual(payload["text"], "hello")
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertTrue(client.response.status_checked)

    async def test_send_message_splits_on_line_boundaries(self) -> None:
        client = FakeAsyncClient()
        gateway = TelegramGateway("token", "chat", client)

        await gateway.send_message(("x" * 3000) + "\n" + ("y" * 3000))

        self.assertEqual(len(client.posts), 2)
        self.assertLessEqual(len(client.posts[0][1]["text"]), 4096)
        self.assertLessEqual(len(client.posts[1][1]["text"]), 4096)

    def test_split_message_rejects_single_line_over_limit(self) -> None:
        with self.assertRaises(ValueError):
            _split_message("x" * 4097)

    def test_retryable_telegram_error_detection(self) -> None:
        request = httpx.Request("POST", "https://api.telegram.org")
        too_many_requests = httpx.Response(429, request=request)
        bad_request = httpx.Response(400, request=request)

        self.assertTrue(
            _is_retryable_telegram_error(
                httpx.HTTPStatusError(
                    "rate limited", request=request, response=too_many_requests
                )
            )
        )
        self.assertFalse(
            _is_retryable_telegram_error(
                httpx.HTTPStatusError("bad", request=request, response=bad_request)
            )
        )
        self.assertTrue(_is_retryable_telegram_error(httpx.TimeoutException("slow")))


if __name__ == "__main__":
    unittest.main()
