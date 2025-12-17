import logging
from typing import List, Optional

import httpx

from config.settings import SMSSettings


logger = logging.getLogger(__name__)


class SMSClient:
    def __init__(self, settings: SMSSettings):
        self.api_key = settings.api_key
        self.sender = settings.sender
        self.admins = settings.admins
        self.client = httpx.AsyncClient(timeout=10.0)

    async def close(self) -> None:
        await self.client.aclose()

    async def send_message(self, text: str, to_override: Optional[List[str]] = None) -> None:
        recipients = to_override or self.admins
        if not recipients:
            logger.warning("No SMS recipients configured; skipping send.")
            return
        payload = {
            "from": self.sender,
            "to": recipients,
            "text": text,
            "udh": "",
        }
        url = f"https://console.melipayamak.com/api/send/advanced/{self.api_key}"
        resp = await self.client.post(url, json=payload)
        try:
            resp.raise_for_status()
        except Exception as exc:
            logger.error("SMS send failed: %s body=%s", exc, resp.text)
            raise
        logger.info("SMS send response: %s", resp.json() if resp.content else resp.status_code)
