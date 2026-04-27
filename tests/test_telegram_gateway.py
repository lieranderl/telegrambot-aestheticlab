import unittest

from src.gateways.telegram_api import TelegramGateway


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

    async def test_send_message_posts_and_truncates(self) -> None:
        client = FakeAsyncClient()
        gateway = TelegramGateway("token", "chat", client)

        await gateway.send_message("x" * 5000)

        url, payload = client.posts[0]
        self.assertIn("/bottoken/sendMessage", url)
        self.assertEqual(payload["chat_id"], "chat")
        self.assertEqual(len(payload["text"]), 4091)
        self.assertTrue(client.response.status_checked)


if __name__ == "__main__":
    unittest.main()
