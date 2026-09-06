from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from azure.core.credentials import AccessToken
from azure.identity import CredentialUnavailableError
from websockets.exceptions import InvalidStatus

from src import realtime_conversation as conversation


class RealtimeAuthorizationTests(unittest.TestCase):
    def setUp(self):
        cache = patch.object(conversation, "_cached_realtime_token", None)
        cache.start()
        self.addCleanup(cache.stop)
        clock = patch.object(conversation.time, "time", return_value=1000)
        clock.start()
        self.addCleanup(clock.stop)
        self.credential = Mock(get_token=AsyncMock(), close=AsyncMock())
        self.credential.get_token.return_value = AccessToken("test-token", 4600)
        factory = patch.object(conversation, "GameCredential", return_value=self.credential)
        self.factory = factory.start()
        self.addCleanup(factory.stop)

    async def authorize(self):
        async with conversation._realtime_authorization() as headers:
            return headers

    def test_second_patient_reuses_token_across_event_loops(self):
        first = asyncio.run(self.authorize())
        second = asyncio.run(self.authorize())
        self.assertEqual(first, second)
        self.assertEqual(second, {"Authorization": "Bearer test-token"})
        self.factory.assert_called_once_with(sign_in=False)
        self.credential.get_token.assert_awaited_once_with(conversation.AZURE_OPENAI_SCOPE)
        self.credential.close.assert_awaited_once()

    def test_refreshes_expiring_and_expired_tokens(self):
        for expires_on in (999, 1000, 1300):
            with self.subTest(expires_on=expires_on):
                conversation._cached_realtime_token = AccessToken("old-token", expires_on)
                headers = asyncio.run(self.authorize())
                self.assertEqual(headers, {"Authorization": "Bearer test-token"})
        self.assertEqual(self.credential.get_token.await_count, 3)

    def test_token_outside_refresh_margin_avoids_cli(self):
        conversation._cached_realtime_token = AccessToken("valid-token", 1301)
        self.assertEqual(asyncio.run(self.authorize()), {"Authorization": "Bearer valid-token"})
        self.factory.assert_not_called()

    def test_timeout_does_not_use_expired_token_and_retry_can_succeed(self):
        conversation._cached_realtime_token = AccessToken("expired-token", 999)
        self.credential.get_token.side_effect = [
            CredentialUnavailableError("Timed out waiting for Azure CLI"),
            AccessToken("retry-token", 4600),
        ]
        with self.assertRaisesRegex(CredentialUnavailableError, "Timed out"):
            asyncio.run(self.authorize())
        self.credential.close.assert_awaited_once()
        self.assertEqual(asyncio.run(self.authorize()), {"Authorization": "Bearer retry-token"})
        self.assertEqual(self.credential.close.await_count, 2)

    def test_rejected_token_is_refreshed_on_retry(self):
        async def rejected():
            async with conversation._realtime_authorization():
                raise InvalidStatus(Mock(status_code=401))

        with self.assertRaises(InvalidStatus):
            asyncio.run(rejected())
        self.assertIsNone(conversation._cached_realtime_token)
        asyncio.run(self.authorize())
        self.assertEqual(self.credential.get_token.await_count, 2)

    def test_non_auth_failure_keeps_valid_token(self):
        async def unavailable():
            async with conversation._realtime_authorization():
                raise ConnectionError("offline")

        with self.assertRaises(ConnectionError):
            asyncio.run(unavailable())
        asyncio.run(self.authorize())
        self.credential.get_token.assert_awaited_once()

    def test_scoring_http_401_refreshes_token_but_403_keeps_it(self):
        async def failed_request(status_code):
            async with conversation._realtime_authorization():
                response = httpx.Response(status_code, request=httpx.Request("POST", "https://example.test/score"))
                response.raise_for_status()

        for status_code, cleared in ((403, False), (401, True)):
            with self.subTest(status_code=status_code), self.assertRaises(httpx.HTTPStatusError):
                asyncio.run(failed_request(status_code))
            self.assertEqual(conversation._cached_realtime_token is None, cleared)
        asyncio.run(self.authorize())
        self.assertEqual(self.credential.get_token.await_count, 2)

    def test_cancelled_auth_closes_credential_without_caching(self):
        async def cancelled():
            started = asyncio.Event()

            async def acquire(*args):
                started.set()
                await asyncio.Event().wait()

            self.credential.get_token.side_effect = acquire
            task = asyncio.create_task(self.authorize())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        asyncio.run(cancelled())
        self.credential.close.assert_awaited_once()
        self.assertIsNone(conversation._cached_realtime_token)


if __name__ == "__main__":
    unittest.main()