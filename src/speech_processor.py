"""Cancellable subprocess boundary: optional scientific dependencies stay off the game loop."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import struct
import sys
from dataclasses import asdict
from pathlib import Path

from src.audio_playback import AudioPlaybackError
from src.voice_profile import VoiceProfile

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MAX_PCM_BYTES = 24_000 * 2 * 30


class SpeechProcessor:
    def __init__(self, profile: VoiceProfile, *, persistent: bool = False) -> None:
        self.profile = profile
        self.persistent = persistent
        self._process: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task[str] | None = None
        self._lock = asyncio.Lock()
        if profile.enabled:
            missing = [name for name in ("numpy", "pyworld", "pkg_resources")
                       if importlib.util.find_spec(name) is None]
            if missing:
                raise AudioPlaybackError(
                    "NPC voice processing dependencies missing: " + ", ".join(missing)
                    + ". Run uv sync --locked from the repository root and launch with uv run, "
                    "or set performance_profile.voice.enabled to false."
                )

    async def __call__(self, pcm: bytes) -> bytes:
        if not self.profile.enabled:
            return pcm
        if not pcm or len(pcm) % 2 or len(pcm) > MAX_PCM_BYTES:
            raise AudioPlaybackError("NPC speech must be nonempty PCM16 and at most 30 seconds")
        async with self._lock:
            succeeded = False
            try:
                if self._process is None or self._process.returncode is not None:
                    await self._close_worker()
                    try:
                        self._process = await asyncio.create_subprocess_exec(
                            sys.executable, "-m", "src.voice_worker", "--stream",
                            "--profile", json.dumps(asdict(self.profile)),
                            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE, cwd=ROOT,
                        )
                    except OSError as error:
                        raise AudioPlaybackError(f"Could not start NPC voice processor: {error}") from error
                    assert self._process.stderr is not None
                    self._stderr_task = asyncio.create_task(self._read_stderr(self._process.stderr))
                try:
                    stdout = await asyncio.wait_for(self._exchange(pcm), timeout=30)
                except asyncio.TimeoutError as error:
                    raise AudioPlaybackError("NPC voice processing timed out; shorten the reply or disable the effect") from error
                header, separator, audio = stdout.partition(b"\n")
                try:
                    metadata = json.loads(header)
                except (ValueError, UnicodeDecodeError) as error:
                    raise AudioPlaybackError("Invalid NPC voice processor metadata") from error
                if (not separator or len(audio) != len(pcm) or not isinstance(metadata, dict)
                        or metadata.get("status") not in
                        ("processed", "unchanged_quiet", "unchanged_unvoiced", "disabled")):
                    raise AudioPlaybackError("Invalid NPC voice output or changed duration")
                if metadata["status"].startswith("unchanged_"):
                    LOGGER.warning("NPC voice kept original speech: %s", metadata)
                else:
                    LOGGER.info("NPC voice processing: %s", metadata)
                succeeded = True
                return audio
            finally:
                if not self.persistent or not succeeded:
                    await self._close_worker()

    @staticmethod
    async def _read_stderr(stream: asyncio.StreamReader) -> str:
        tail = b""
        while chunk := await stream.read(4096):
            tail = (tail + chunk)[-3000:]
        return tail.decode("utf-8", errors="replace")

    async def _exchange(self, pcm: bytes) -> bytes:
        process = self._process
        assert process is not None and process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(struct.pack("!I", len(pcm)) + pcm)
            await process.stdin.drain()
            header = await process.stdout.readexactly(4)
            size = struct.unpack("!I", header)[0]
            if not len(pcm) < size <= len(pcm) + 4096:
                raise AudioPlaybackError("Invalid NPC voice output or changed duration")
            return await process.stdout.readexactly(size)
        except (BrokenPipeError, ConnectionResetError, asyncio.IncompleteReadError) as error:
            await process.wait()
            detail = await self._stderr_task if self._stderr_task is not None else ""
            raise AudioPlaybackError("NPC voice processing failed: " + detail) from error

    async def aclose(self) -> None:
        async with self._lock:
            await self._close_worker()

    async def _close_worker(self) -> None:
        process, self._process = self._process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        if process is None:
            return
        try:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
        finally:
            if process.stdin is not None:
                process.stdin.close()
            if stderr_task is not None:
                await asyncio.gather(stderr_task, return_exceptions=True)
