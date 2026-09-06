from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError
from azure.identity import CredentialUnavailableError

from src import azure_auth


class GameCredentialTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cli = AsyncMock()
        self.cli_patch = patch.object(azure_auth, "AzureCliCredential", return_value=self.cli)
        self.cli_patch.start()
        self.addCleanup(self.cli_patch.stop)
        self.cache_patch = patch.object(azure_auth, "_browser_tokens", {})
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    async def test_existing_cli_login_still_works(self):
        token = AccessToken("cli-token", 9999999999)
        self.cli.get_token.return_value = token
        credential = azure_auth.GameCredential()
        self.assertEqual(await credential.get_token("scope"), token)
        self.cli.get_token.assert_awaited_once_with("scope")
        await credential.close()
        self.cli.close.assert_awaited_once()

    async def test_missing_cli_and_expired_login_request_sign_in(self):
        for error in (CredentialUnavailableError("missing"), ClientAuthenticationError("expired")):
            with self.subTest(error=type(error).__name__):
                self.cli.get_token.side_effect = error
                with self.assertRaises(azure_auth.AzureSignInRequired):
                    await azure_auth.GameCredential().get_token("scope")

    async def test_browser_token_is_reused_across_consultations(self):
        token = AccessToken("browser-token", 9999999999)
        with patch.object(azure_auth, "_browser_access_token", new=AsyncMock(return_value=token)) as browser:
            self.assertEqual(await azure_auth.GameCredential(sign_in=True).get_token("scope"), token)
            self.assertEqual(await azure_auth.GameCredential().get_token("scope"), token)
        browser.assert_awaited_once_with("scope")
        self.cli.get_token.assert_not_awaited()

    async def test_expiring_token_and_different_scope_are_not_reused(self):
        azure_auth._browser_tokens["scope"] = AccessToken("old", 110)
        with patch.object(azure_auth.time, "time", return_value=100):
            await azure_auth.GameCredential().get_token("scope")
            await azure_auth.GameCredential().get_token("other-scope")
        self.assertEqual(self.cli.get_token.await_count, 2)

    async def test_explicit_sign_in_replaces_cached_account(self):
        azure_auth._browser_tokens["scope"] = AccessToken("previous", 9999999999)
        token = AccessToken("replacement", 9999999999)
        with patch.object(azure_auth, "_browser_access_token", new=AsyncMock(return_value=token)):
            self.assertEqual(await azure_auth.GameCredential(sign_in=True).get_token("scope"), token)
        self.assertEqual(azure_auth._browser_tokens["scope"], token)

    async def test_cli_programming_error_is_not_hidden(self):
        self.cli.get_token.side_effect = TypeError("bug")
        with self.assertRaises(TypeError):
            await azure_auth.GameCredential().get_token("scope")


class BrowserWorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.process = Mock()
        self.process.returncode = 0
        self.process.communicate = AsyncMock(
            return_value=(json.dumps({"token": "test-token", "expires_on": 9999999999}).encode(), None)
        )
        self.process.wait = AsyncMock()
        self.launch_patch = patch.object(
            azure_auth.asyncio, "create_subprocess_exec",
            new=AsyncMock(return_value=self.process),
        )
        self.launch = self.launch_patch.start()
        self.addCleanup(self.launch_patch.stop)

    async def test_browser_result_is_read_from_private_pipe(self):
        token = await azure_auth._browser_access_token("scope")
        self.assertEqual(token, AccessToken("test-token", 9999999999))
        self.assertEqual(self.launch.call_args.kwargs["stdout"], asyncio.subprocess.PIPE)
        self.assertEqual(self.launch.call_args.kwargs["stderr"], asyncio.subprocess.DEVNULL)
        self.process.wait.assert_awaited_once()
        self.process.kill.assert_not_called()

    async def test_sign_in_failure_is_actionable(self):
        self.process.returncode = 1
        with self.assertRaises(azure_auth.AzureSignInRequired):
            await azure_auth._browser_access_token("scope")

    async def test_unexpected_worker_error_is_not_hidden(self):
        self.process.returncode = 2
        with self.assertRaises(RuntimeError):
            await azure_auth._browser_access_token("scope")

    async def test_invalid_worker_result_is_rejected(self):
        for payload in ({}, {"token": "", "expires_on": 100}, {"token": "test", "expires_on": "100"}):
            self.process.communicate.return_value = (json.dumps(payload).encode(), None)
            with self.subTest(payload=payload), self.assertRaises(RuntimeError):
                await azure_auth._browser_access_token("scope")

    async def test_timeout_terminates_browser_listener(self):
        self.process.returncode = None
        self.process.communicate.side_effect = TimeoutError
        with self.assertRaises(azure_auth.AzureSignInRequired):
            await azure_auth._browser_access_token("scope")
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()

    async def test_cancellation_terminates_browser_listener(self):
        self.process.returncode = None
        started = asyncio.Event()

        async def communicate():
            started.set()
            await asyncio.Event().wait()

        self.process.communicate.side_effect = communicate
        task = asyncio.create_task(azure_auth._browser_access_token("scope"))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.process.kill.assert_called_once()
        self.process.wait.assert_awaited_once()


class BrowserEntryPointTests(unittest.TestCase):
    def test_sdk_browser_credential_uses_requested_scope(self):
        with patch.object(azure_auth.sys, "argv", ["azure_auth.py", "scope"]), patch.object(
            azure_auth, "InteractiveBrowserCredential"
        ) as browser, patch("builtins.print") as output:
            credential = browser.return_value.__enter__.return_value
            credential.get_token.return_value = AccessToken("test-token", 1234)
            self.assertEqual(azure_auth.main(), 0)
            credential.get_token.assert_called_once_with("scope")
            browser.assert_called_once_with(timeout=azure_auth.BROWSER_TIMEOUT_SECONDS)
            self.assertEqual(json.loads(output.call_args.args[0]), {"token": "test-token", "expires_on": 1234})
            browser.return_value.__exit__.assert_called_once()

    def test_sdk_failure_does_not_print_credentials(self):
        with patch.object(azure_auth.sys, "argv", ["azure_auth.py", "scope"]), patch.object(
            azure_auth, "InteractiveBrowserCredential"
        ) as browser, patch("builtins.print") as output:
            browser.return_value.__enter__.return_value.get_token.side_effect = ClientAuthenticationError("private detail")
            self.assertEqual(azure_auth.main(), 1)
            output.assert_not_called()


if __name__ == "__main__":
    unittest.main()
