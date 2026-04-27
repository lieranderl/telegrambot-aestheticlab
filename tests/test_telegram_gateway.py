import unittest

import httpx

from src.gateways.telegram_api import (
    TelegramDeliveryError,
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

    async def test_send_message_sanitizes_http_status_error(self) -> None:
        token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
        request = httpx.Request(
            "POST", f"https://api.telegram.org/bot{token}/sendMessage"
        )
        response = httpx.Response(
            500,
            request=request,
            text="telegram unavailable",
        )

        class FailingResponse:
            def raise_for_status(self) -> None:
                raise httpx.HTTPStatusError(
                    "server error",
                    request=request,
                    response=response,
                )

        client = FakeAsyncClient()
        client.response = FailingResponse()
        gateway = TelegramGateway(token, "chat", client)

        with self.assertRaises(TelegramDeliveryError) as ctx:
            await gateway.send_message("hello")

        self.assertEqual(
            str(ctx.exception), "Telegram API request failed with status 500"
        )
        self.assertNotIn(token, str(ctx.exception))

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
