import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

import requests

from config.settings import ViraSettings


logger = logging.getLogger(__name__)


@dataclass
class STTResult:
    status: str
    text: str
    request_id: Optional[str] = None
    trace_id: Optional[str] = None


class ViraSTTClient:
    """
    Async Vira STT wrapper with concurrency control.
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

    async def close(self) -> None:
        return

    async def transcribe_audio(
        self,
        audio_bytes: bytes,
        language_model: str = "default",
        hotwords: Optional[list[str]] = None,
    ) -> STTResult:
        token = self.settings.stt_token
        if not token:
            logger.warning("Vira STT token is missing; STT call skipped.")
            return STTResult(status="unauthorized", text="")

        headers = {
            "gateway-token": token,
            "accept": "application/json",
        }
        files = {
            "audio": ("audio.wav", audio_bytes, "audio/wav"),
        }
        data_list = [
            ("model", language_model),
            ("srt", "false"),
            ("inverseNormalizer", "false"),
            ("timestamp", "false"),
            ("spokenPunctuation", "false"),
            ("punctuation", "false"),
            ("numSpeakers", "0"),
            ("diarize", "false"),
        ]
        if hotwords:
            for word in hotwords:
                data_list.append(("hotwords[]", word))

        async with self.semaphore:
            response = await asyncio.to_thread(
                self._post_sync,
                headers,
                data_list,
                audio_bytes,
            )
        response.raise_for_status()
        payload = response.json()
        data_section = payload.get("data", {}) or {}
        nested_data = data_section.get("data", {}) or {}
        ai_response = nested_data.get("aiResponse", {}) or {}
        ai_result = ai_response.get("result", {}) or {}

        text = (
            data_section.get("text")
            or nested_data.get("text")
            or ai_result.get("text")
            or ""
        )
        status = (
            data_section.get("status")
            or payload.get("status")
            or ai_response.get("status")
            or "unknown"
        )
        request_id = (
            data_section.get("requestId")
            or nested_data.get("requestId")
            or ai_response.get("requestId")
        )
        trace_id = (
            data_section.get("traceId")
            or nested_data.get("traceId")
            or ai_response.get("meta", {}).get("traceId")
        )

        if not text:
            logger.warning("Vira STT returned empty text. status=%s payload=%s", status, payload)

        return STTResult(status=status, text=text, request_id=request_id, trace_id=trace_id)

    def _post_sync(self, headers: dict, data_list: list, audio_bytes: bytes) -> requests.Response:
        files = {"audio": ("audio.wav", audio_bytes, "audio/wav")}
        return requests.post(
            self.settings.stt_url,
            headers=headers,
            data=data_list,
            files=files,
            timeout=self.timeout,
            verify=self.settings.verify_ssl,
        )
