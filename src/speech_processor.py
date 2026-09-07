"""Cancellable subprocess boundary: optional scientific dependencies stay off the game loop."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path

from src.audio_playback import AudioPlaybackError
from src.voice_profile import VoiceProfile

LOGGER = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
MAX_PCM_BYTES = 24_000 * 2 * 30


class SpeechProcessor:
    def __init__(self, profile: VoiceProfile) -> None:
        self.profile = profile
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
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "src.voice_worker",
                "--profile", json.dumps(asdict(self.profile)),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, cwd=ROOT,
            )
        except OSError as error:
            raise AudioPlaybackError(f"Could not start NPC voice processor: {error}") from error
        try:
            try:
                stdout, stderr = await asyncio.wait_for(process.communicate(pcm), timeout=30)
            except TimeoutError as error:
                raise AudioPlaybackError("NPC voice processing timed out; shorten the reply or disable the effect") from error
            if process.returncode != 0:
                raise AudioPlaybackError(
                    "NPC voice processing failed: " + stderr.decode("utf-8", errors="replace")[-3000:]
                )
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
            return audio
        finally:
            # Cancelling communicate does not kill its child. Reap this specific process on every exit.
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                except TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                    await process.wait()
