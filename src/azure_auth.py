from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

from azure.core.credentials import AccessToken
from azure.core.exceptions import AzureError, ClientAuthenticationError
from azure.identity import InteractiveBrowserCredential
from azure.identity.aio import AzureCliCredential


BROWSER_TIMEOUT_SECONDS = 180
_browser_tokens: dict[str, AccessToken] = {}


def clear_cached_token(scope: str) -> None:
    _browser_tokens.pop(scope, None)


class AzureSignInRequired(ClientAuthenticationError):
    pass


async def _browser_access_token(scope: str) -> AccessToken:
    # Isolate the blocking SDK browser flow so cancellation also closes its listener.
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        str(Path(__file__).resolve()),
        scope,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        try:
            output, _ = await asyncio.wait_for(
                process.communicate(), timeout=BROWSER_TIMEOUT_SECONDS + 30
            )
        except TimeoutError as error:
            raise AzureSignInRequired(
                "Sign-in timed out. Select Sign in to Azure to try again."
            ) from error
        if process.returncode == 1:
            raise AzureSignInRequired(
                "Sign-in did not finish. Check your browser and network, then try again."
            )
        if process.returncode != 0:
            raise RuntimeError("The Azure sign-in helper exited unexpectedly.")
        payload = json.loads(output)
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("token"), str)
            or not payload["token"]
            or type(payload.get("expires_on")) is not int
        ):
            raise RuntimeError("The Azure sign-in helper returned an invalid token response.")
        return AccessToken(payload["token"], payload["expires_on"])
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


class GameCredential:
    def __init__(self, *, sign_in: bool = False) -> None:
        self._sign_in = sign_in
        self._cli = AzureCliCredential(process_timeout=60)

    async def get_token(self, scope: str) -> AccessToken:
        if self._sign_in:
            token = await _browser_access_token(scope)
            _browser_tokens[scope] = token
            return token
        cached = _browser_tokens.get(scope)
        if cached is not None and cached.expires_on > time.time() + 300:
            return cached
        try:
            return await self._cli.get_token(scope)
        except ClientAuthenticationError as error:
            raise AzureSignInRequired(
                "Sign in with a Microsoft work or school account that has access to this Azure resource."
            ) from error

    async def close(self) -> None:
        await self._cli.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scope")
    arguments = parser.parse_args()
    try:
        with InteractiveBrowserCredential(timeout=BROWSER_TIMEOUT_SECONDS) as credential:
            token = credential.get_token(arguments.scope)
    except (AzureError, OSError):
        return 1
    # The parent reads this private pipe; tokens never go to the game log or disk.
    print(json.dumps({"token": token.token, "expires_on": token.expires_on}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
