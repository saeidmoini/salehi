import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

import httpx


logger = logging.getLogger(__name__)


@dataclass
class PanelNumber:
    id: int
    phone_number: str


@dataclass
class PanelAgent:
    id: Optional[int]
    phone_number: str


@dataclass
class PanelOutboundLine:
    id: Optional[int]
    phone_number: str
    display_name: Optional[str] = None


@dataclass
class NextBatchResponse:
    call_allowed: bool
    retry_after_seconds: Optional[int]
    numbers: List[PanelNumber]
    agents: List[PanelAgent]
    inbound_agents: List[PanelAgent]
    outbound_agents: List[PanelAgent]
    active_scenarios: Optional[List[str]]
    outbound_lines: List[PanelOutboundLine]
    batch_id: Optional[str]
    timezone: Optional[str]
    server_time: Optional[datetime]
    schedule_version: Optional[int]
    reason: Optional[str] = None


class PanelClient:
    def __init__(
        self,
        base_url: str,
        api_token: str,
        company: str = "",
        timeout: float = 10.0,
        max_connections: int = 20,
        default_retry: int = 60,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.company = company
        self.default_retry = default_retry
        limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_connections)
        self.client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            limits=limits,
            headers={"Authorization": f"Bearer {api_token}"},
        )
        self.pending_reports: list[dict] = []
        self.lock = asyncio.Lock()

    async def close(self) -> None:
        await self.client.aclose()

    def _company_params(self, extra: Optional[dict] = None) -> dict:
        """Build query params including company identifier."""
        params = {}
        if self.company:
            params["company"] = self.company
        if extra:
            params.update(extra)
        return params

    async def get_next_batch(self, size: int) -> NextBatchResponse:
        empty = NextBatchResponse(
            call_allowed=False,
            retry_after_seconds=self.default_retry,
            numbers=[], agents=[], inbound_agents=[], outbound_agents=[],
            active_scenarios=None, outbound_lines=[], batch_id=None, timezone=None,
            server_time=None, schedule_version=None,
        )
        try:
            await self.flush_pending()
            params = self._company_params({"size": size})
            resp = await self.client.get("/api/dialer/next-batch", params=params)
            resp.raise_for_status()
            data = resp.json()
            if not data.get("call_allowed", False):
                retry = data.get("retry_after_seconds") or self.default_retry
                empty.retry_after_seconds = retry
                empty.timezone = data.get("timezone")
                empty.server_time = self._parse_dt(data.get("server_time"))
                empty.schedule_version = data.get("schedule_version")
                empty.reason = data.get("reason")
                # Still parse scenario/line metadata even when call_allowed=False
                empty.active_scenarios = self._parse_active_scenarios(data.get("active_scenarios"))
                empty.outbound_lines = self._parse_outbound_lines(data.get("outbound_lines"))
                return empty
            batch = data.get("batch", {}) or {}
            numbers = [
                PanelNumber(id=item["id"], phone_number=item["phone_number"])
                for item in batch.get("numbers", []) or []
            ]
            # Legacy: active_agents (backward compatible)
            agents = [
                PanelAgent(id=agent.get("id"), phone_number=agent.get("phone_number", ""))
                for agent in data.get("active_agents", []) or []
                if agent.get("phone_number")
            ]
            # New: separate inbound/outbound agent lists
            inbound_agents = [
                PanelAgent(id=agent.get("id"), phone_number=agent.get("phone_number", ""))
                for agent in data.get("inbound_agents", []) or []
                if agent.get("phone_number")
            ]
            outbound_agents = [
                PanelAgent(id=agent.get("id"), phone_number=agent.get("phone_number", ""))
                for agent in data.get("outbound_agents", []) or []
                if agent.get("phone_number")
            ]
            active_scenarios = self._parse_active_scenarios(data.get("active_scenarios"))
            outbound_lines = self._parse_outbound_lines(data.get("outbound_lines"))
            return NextBatchResponse(
                call_allowed=True,
                retry_after_seconds=None,
                numbers=numbers,
                agents=agents,
                inbound_agents=inbound_agents,
                outbound_agents=outbound_agents,
                active_scenarios=active_scenarios,
                outbound_lines=outbound_lines,
                batch_id=batch.get("batch_id"),
                timezone=data.get("timezone"),
                server_time=self._parse_dt(data.get("server_time")),
                schedule_version=data.get("schedule_version"),
                reason=None,
            )
        except Exception as exc:
            logger.error("Panel get_next_batch failed: %s", exc)
            empty.reason = str(exc)
            return empty

    async def report_result(
        self,
        number_id: Optional[int],
        phone_number: Optional[str],
        status: str,
        reason: str,
        attempted_at: datetime,
        batch_id: Optional[str] = None,
        call_allowed: Optional[bool] = None,
        agent_id: Optional[int] = None,
        agent_phone: Optional[str] = None,
        user_message: Optional[str] = None,
        scenario: Optional[str] = None,
        outbound_line: Optional[str] = None,
    ) -> None:
        payload = {
            "number_id": number_id,
            "phone_number": phone_number,
            "status": status,
            "reason": reason,
            "attempted_at": attempted_at.replace(tzinfo=timezone.utc).isoformat(),
        }
        if self.company:
            payload["company"] = self.company
        if batch_id:
            payload["batch_id"] = batch_id
        if call_allowed is not None:
            payload["call_allowed"] = call_allowed
        if agent_id is not None:
            payload["agent_id"] = agent_id
        if agent_phone:
            payload["agent_phone"] = agent_phone
        if user_message:
            payload["user_message"] = user_message
        if scenario:
            payload["scenario"] = scenario
        if outbound_line:
            payload["outbound_line"] = outbound_line
        try:
            resp = await self.client.post("/api/dialer/report-result", json=payload)
            resp.raise_for_status()
            logger.info("Reported result to panel number_id=%s status=%s", number_id, status)
        except Exception as exc:
            logger.warning("Failed to report result to panel; queueing. err=%s payload=%s", exc, payload)
            async with self.lock:
                self.pending_reports.append(payload)

    async def register_scenarios(self, scenario_names: List[str]) -> None:
        """Register available scenarios with the panel."""
        if not scenario_names:
            return
        payload = {"scenarios": scenario_names}
        if self.company:
            payload["company"] = self.company
        try:
            resp = await self.client.post("/api/dialer/register-scenarios", json=payload)
            resp.raise_for_status()
            logger.info("Registered scenarios with panel: %s", scenario_names)
        except Exception as exc:
            logger.warning("Failed to register scenarios with panel: %s", exc)

    async def flush_pending(self) -> None:
        async with self.lock:
            if not self.pending_reports:
                return
            queued = list(self.pending_reports)
            self.pending_reports.clear()
        for payload in queued:
            if not payload.get("number_id") and not payload.get("phone_number"):
                logger.debug("Dropping queued panel report without number/phone: %s", payload)
                continue
            try:
                resp = await self.client.post("/api/dialer/report-result", json=payload)
                resp.raise_for_status()
                logger.info("Flushed queued report to panel number_id=%s", payload.get("number_id"))
            except Exception as exc:
                logger.warning("Failed to flush queued report; requeue. err=%s payload=%s", exc, payload)
                async with self.lock:
                    self.pending_reports.append(payload)
                break

    @staticmethod
    def _parse_dt(value: Optional[str]) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None

    @staticmethod
    def _parse_active_scenarios(value) -> Optional[List[str]]:
        if value is None:
            return None
        names: List[str] = []
        for item in value or []:
            if isinstance(item, str):
                name = item.strip()
            elif isinstance(item, dict):
                name = str(item.get("name", "")).strip()
            else:
                name = ""
            if name:
                names.append(name)
        return names

    @staticmethod
    def _parse_outbound_lines(value) -> List[PanelOutboundLine]:
        lines: List[PanelOutboundLine] = []
        for item in value or []:
            if not isinstance(item, dict):
                continue
            phone = str(item.get("phone_number", "")).strip()
            if not phone:
                continue
            line_id = item.get("id")
            display_name = item.get("display_name")
            lines.append(PanelOutboundLine(id=line_id, phone_number=phone, display_name=display_name))
        return lines
