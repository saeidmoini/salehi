import logging
from typing import Any, Dict, Optional

import httpx

from config.settings import AriSettings


logger = logging.getLogger(__name__)


class AriClient:
    """
    Thin async wrapper around the Asterisk ARI HTTP endpoints.
    """

    def __init__(
        self,
        settings: AriSettings,
        timeout: float = 10.0,
        max_connections: int = 100,
    ):
        self.base_url = settings.base_url.rstrip("/")
        self.app_name = settings.app_name
        self.auth = (settings.username, settings.password)
        limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        )
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            auth=self.auth,
            headers={"Accept": "application/json"},
            timeout=timeout,
            limits=limits,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        logger.debug("ARI %s %s params=%s json=%s", method, path, params, json)
        response = await self.client.request(
            method=method,
            url=path,
            params=params,
            json=json,
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    async def create_bridge(self, name: str, bridge_type: str = "mixing") -> Dict[str, Any]:
        return await self._request(
            "POST",
            "/bridges",
            params={"type": bridge_type, "name": name},
        )

    async def delete_bridge(self, bridge_id: str) -> None:
        await self._request("DELETE", f"/bridges/{bridge_id}")

    async def add_channel_to_bridge(
        self, bridge_id: str, channel_id: str, role: Optional[str] = None
    ) -> None:
        params = {"channel": channel_id}
        if role:
            params["role"] = role
        await self._request("POST", f"/bridges/{bridge_id}/addChannel", params=params)

    async def remove_channel_from_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self._request(
            "POST", f"/bridges/{bridge_id}/removeChannel", params={"channel": channel_id}
        )

    async def answer_channel(self, channel_id: str) -> None:
        await self._request("POST", f"/channels/{channel_id}/answer")

    async def hangup_channel(self, channel_id: str, reason: str = "normal") -> None:
        await self._request(
            "DELETE", f"/channels/{channel_id}", params={"reason": reason}
        )

    async def play_on_channel(
        self, channel_id: str, media: str, lang: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"media": media}
        if lang:
            params["lang"] = lang
        return await self._request(
            "POST", f"/channels/{channel_id}/play", params=params
        )

    async def play_on_bridge(
        self, bridge_id: str, media: str, lang: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"media": media}
        if lang:
            params["lang"] = lang
        return await self._request(
            "POST", f"/bridges/{bridge_id}/play", params=params
        )

    async def originate_call(
        self,
        endpoint: str,
        app_args: str,
        caller_id: Optional[str] = None,
        timeout: int = 30,
        variables: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "endpoint": endpoint,
            "app": self.app_name,
            "appArgs": app_args,
            "timeout": timeout,
        }
        if caller_id:
            params["callerId"] = caller_id
        if variables:
            params["variables"] = variables

        logger.info(
            "Originating call endpoint=%s appArgs=%s callerId=%s timeout=%s",
            endpoint,
            app_args,
            caller_id,
            timeout,
        )
        return await self._request("POST", "/channels", params=params)

    async def stop_playback(self, playback_id: str) -> None:
        await self._request("DELETE", f"/playbacks/{playback_id}")

    async def record_channel(
        self,
        channel_id: str,
        name: str,
        max_duration: int = 8,
        max_silence: int = 3,
        fmt: str = "wav",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "name": name,
            "format": fmt,
            "maxDurationSeconds": max_duration,
            "maxSilenceSeconds": max_silence,
            "ifExists": "overwrite",
            "beep": "false",
        }
        return await self._request(
            "POST", f"/channels/{channel_id}/record", params=params
        )

    async def get_channel_variable(self, channel_id: str, variable: str) -> Optional[str]:
        try:
            resp = await self._request(
                "GET", f"/channels/{channel_id}/variable", params={"variable": variable}
            )
            return resp.get("value") if isinstance(resp, dict) else None
        except Exception as exc:
            logger.debug("Failed to fetch channel var %s for %s: %s", variable, channel_id, exc)
            return None

    async def fetch_stored_recording(self, name: str) -> bytes:
        logger.debug("Fetching stored recording %s", name)
        response = await self.client.get(f"/recordings/stored/{name}/file")
        response.raise_for_status()
        return await response.aread()

    async def record_bridge(
        self,
        bridge_id: str,
        name: str,
        max_duration: int = 10,
        max_silence: int = 1,
        fmt: str = "wav",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "name": name,
            "format": fmt,
            "maxDurationSeconds": max_duration,
            "maxSilenceSeconds": max_silence,
            "ifExists": "overwrite",
            "beep": "false",
        }
        return await self._request("POST", f"/bridges/{bridge_id}/record", params=params)
