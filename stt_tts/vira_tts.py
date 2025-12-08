import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import httpx

from config.settings import ViraSettings


logger = logging.getLogger(__name__)


@dataclass
class TTSResult:
    status: str
    filename: Optional[str] = None
    url: Optional[str] = None
    duration: Optional[float] = None


class ViraTTSClient:
    """
    Async Vira TTS wrapper with concurrency control.
    """

    def __init__(
        self,
        settings: ViraSettings,
        timeout: float = 30.0,
        max_connections: int = 100,
        semaphore: Optional[asyncio.Semaphore] = None,
    ):
        self.settings = settings
        self.timeout = timeout
        self.semaphore = semaphore or asyncio.Semaphore(10)
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        self.client = httpx.AsyncClient(timeout=timeout, limits=limits, verify=settings.verify_ssl)

    async def close(self) -> None:
        await self.client.aclose()

    async def synthesize_text(
        self,
        text: str,
        speaker: str = "female",
        speed: float = 1.0,
    ) -> TTSResult:
        token = self.settings.tts_token
        if not token:
            logger.warning("Vira TTS token is missing; TTS call skipped.")
            return TTSResult(status="unauthorized")

        headers = {
            "gateway-token": token,
            "accept": "application/json",
            "Content-Type": "application/json",
        }
        payload = {"text": text, "speaker": speaker, "speed": speed, "timestamp": False}

        async with self.semaphore:
            response = await self.client.post(
                self.settings.tts_url,
                headers=headers,
                json=payload,
                timeout=self.timeout,
            )
        response.raise_for_status()
        data = response.json()
        result = data.get("data", {})
        return TTSResult(
            status=data.get("status", "unknown"),
            filename=result.get("filename"),
            url=result.get("url"),
            duration=result.get("duration"),
        )
