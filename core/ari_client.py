import logging
from typing import Any, Dict, Optional

import requests

from config.settings import AriSettings


logger = logging.getLogger(__name__)


class AriClient:
    """
    Thin wrapper around the Asterisk ARI HTTP endpoints.
    """

    def __init__(self, settings: AriSettings):
        self.base_url = settings.base_url.rstrip("/")
        self.app_name = settings.app_name
        self.auth = (settings.username, settings.password)
        self.session = requests.Session()
        self.session.auth = self.auth
        self.session.headers.update({"Accept": "application/json"})

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        url = f"{self.base_url}{path}"
        logger.debug("ARI %s %s params=%s json=%s", method, url, params, json)
        response = self.session.request(
            method=method,
            url=url,
            params=params,
            json=json,
            timeout=10,
        )
        response.raise_for_status()
        if response.content:
            return response.json()
        return {}

    def create_bridge(self, name: str, bridge_type: str = "mixing") -> Dict[str, Any]:
        return self._request(
            "POST",
            "/bridges",
            params={"type": bridge_type, "name": name},
        )

    def delete_bridge(self, bridge_id: str) -> None:
        self._request("DELETE", f"/bridges/{bridge_id}")

    def add_channel_to_bridge(
        self, bridge_id: str, channel_id: str, role: Optional[str] = None
    ) -> None:
        params = {"channel": channel_id}
        if role:
            params["role"] = role
        self._request("POST", f"/bridges/{bridge_id}/addChannel", params=params)

    def remove_channel_from_bridge(self, bridge_id: str, channel_id: str) -> None:
        self._request(
            "POST", f"/bridges/{bridge_id}/removeChannel", params={"channel": channel_id}
        )

    def answer_channel(self, channel_id: str) -> None:
        self._request("POST", f"/channels/{channel_id}/answer")

    def hangup_channel(self, channel_id: str, reason: str = "normal") -> None:
        self._request(
            "DELETE", f"/channels/{channel_id}", params={"reason": reason}
        )

    def play_on_channel(
        self, channel_id: str, media: str, lang: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"media": media}
        if lang:
            params["lang"] = lang
        return self._request(
            "POST", f"/channels/{channel_id}/play", params=params
        )

    def play_on_bridge(
        self, bridge_id: str, media: str, lang: Optional[str] = None
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {"media": media}
        if lang:
            params["lang"] = lang
        return self._request(
            "POST", f"/bridges/{bridge_id}/play", params=params
        )

    def originate_call(
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
        return self._request("POST", "/channels", params=params)

    def stop_playback(self, playback_id: str) -> None:
        self._request("DELETE", f"/playbacks/{playback_id}")

    def record_channel(
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
        return self._request(
            "POST", f"/channels/{channel_id}/record", params=params
        )

    def fetch_stored_recording(self, name: str) -> bytes:
        url = f"{self.base_url}/recordings/stored/{name}/file"
        logger.debug("Fetching stored recording %s", name)
        response = self.session.get(url, timeout=15)
        response.raise_for_status()
        return response.content

    def record_bridge(
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
        return self._request("POST", f"/bridges/{bridge_id}/record", params=params)
