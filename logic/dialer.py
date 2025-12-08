import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Deque, List, Optional

from config.settings import Settings
from core.ari_client import AriClient
from integrations.panel.client import NextBatchResponse, PanelClient, PanelNumber
from sessions.session import SessionStatus
from sessions.session_manager import SessionManager


logger = logging.getLogger(__name__)


@dataclass
class ContactItem:
    phone_number: str
    number_id: Optional[int] = None
    batch_id: Optional[str] = None
    attempted_at: Optional[datetime] = None


class Dialer:
    """
    Outbound dialer that enforces concurrency and rate limits.
    """

    def __init__(
        self,
        settings: Settings,
        ari_client: AriClient,
        session_manager: SessionManager,
        panel_client: Optional[PanelClient] = None,
    ):
        self.settings = settings
        self.ari_client = ari_client
        self.session_manager = session_manager
        self.panel_client = panel_client
        self.contacts: Deque[ContactItem] = deque(
            [ContactItem(phone_number=number) for number in settings.dialer.static_contacts]
        )
        self.attempt_timestamps: Deque[datetime] = deque()
        self.daily_counter = 0
        self.daily_marker: date = date.today()
        self._running = False
        self.lock = asyncio.Lock()
        self.next_panel_poll: datetime = datetime.utcnow()
        self.timeout_tasks: dict[str, asyncio.Task] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            return
        self._running = True
        logger.info("Dialer started with %d queued contacts", len(self.contacts))
        try:
            while not stop_event.is_set() and self._running:
                self._reset_daily_if_needed()
                await self._maybe_refill_from_panel()
                if not await self._can_start_call():
                    await asyncio.sleep(1)
                    continue
                contact = await self._next_contact()
                if not contact:
                    await asyncio.sleep(5)
                    continue
                await self._originate(contact)
                await asyncio.sleep(0.2)
        finally:
            self._running = False
            logger.info("Dialer stopped")

    async def stop(self) -> None:
        self._running = False

    async def add_contacts(self, numbers: List[str]) -> None:
        async with self.lock:
            for number in numbers:
                clean = number.strip()
                if clean:
                    self.contacts.append(ContactItem(phone_number=clean))
        logger.info("Queued %d new contacts", len(numbers))

    async def on_session_completed(self, session_id: str) -> None:
        logger.debug("Session %s completed; dialer notified", session_id)

    def _within_call_window(self) -> bool:
        # Panel already enforces schedule; always allow here.
        return True

    def _reset_daily_if_needed(self) -> None:
        today = date.today()
        if today != self.daily_marker:
            logger.info("Resetting daily counters")
            self.daily_counter = 0
            self.daily_marker = today
            self.attempt_timestamps.clear()

    async def _can_start_call(self) -> bool:
        current_sessions = await self.session_manager.active_sessions_count()
        if current_sessions >= self.settings.dialer.max_concurrent_calls:
            return False

        self._prune_attempts()
        if len(self.attempt_timestamps) >= self.settings.dialer.max_calls_per_minute:
            return False

        if self.daily_counter >= self.settings.dialer.max_calls_per_day:
            return False

        return True

    def _prune_attempts(self) -> None:
        cutoff = datetime.utcnow() - timedelta(minutes=1)
        while self.attempt_timestamps and self.attempt_timestamps[0] < cutoff:
            self.attempt_timestamps.popleft()

    async def _next_contact(self) -> Optional[ContactItem]:
        async with self.lock:
            if not self.contacts:
                return None
            return self.contacts.popleft()

    async def _originate(self, contact: ContactItem) -> None:
        try:
            attempted_at = datetime.utcnow()
            contact.attempted_at = attempted_at
            metadata = {"attempted_at": attempted_at.isoformat()}
            if contact.number_id is not None:
                metadata["number_id"] = contact.number_id
            if contact.batch_id:
                metadata["batch_id"] = contact.batch_id
            session = await self.session_manager.create_outbound_session(
                contact_number=contact.phone_number,
                metadata=metadata,
            )
            endpoint = self._build_endpoint(contact)
            app_args = f"outbound,{session.session_id}"
            await self.ari_client.originate_call(
                endpoint=endpoint,
                app_args=app_args,
                caller_id=self.settings.dialer.default_caller_id,
                timeout=self.settings.dialer.origination_timeout,
            )
            self._schedule_timeout_watch(session.session_id)
            self._record_attempt()
            logger.info(
                "Origination requested for %s (session %s)", contact.phone_number, session.session_id
            )
        except Exception as exc:
            logger.exception("Failed to originate call to %s: %s", contact.phone_number, exc)

    def _record_attempt(self) -> None:
        self.attempt_timestamps.append(datetime.utcnow())
        self.daily_counter += 1

    def _build_endpoint(self, contact: ContactItem) -> str:
        trunk = self.settings.dialer.outbound_trunk
        return f"PJSIP/{contact.phone_number}@{trunk}"

    def _schedule_timeout_watch(self, session_id: str) -> None:
        # If no events arrive (no answer/hangup), mark as missed after origination timeout + buffer.
        timeout = self.settings.dialer.origination_timeout + 15
        task = asyncio.create_task(self._mark_missed_if_no_events(session_id, timeout))
        self.timeout_tasks[session_id] = task

    async def _mark_missed_if_no_events(self, session_id: str, delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            session = await self.session_manager.get_session(session_id)
            if not session:
                return
            async with session.lock:
                if session.status == SessionStatus.COMPLETED:
                    return
                if session.result:
                    return
                session.result = "missed"
            if self.session_manager.scenario_handler:
                try:
                    await self.session_manager.scenario_handler.on_call_finished(session)
                except Exception as exc:
                    logger.exception("Failed to report missed for session %s: %s", session_id, exc)
            await self.session_manager._cleanup_session(session)  # type: ignore[attr-defined]
            logger.warning("Marked session %s as missed due to timeout/no events", session_id)
        finally:
            self.timeout_tasks.pop(session_id, None)

    async def _maybe_refill_from_panel(self) -> None:
        if not self.panel_client:
            return
        now = datetime.utcnow()
        if now < self.next_panel_poll:
            return

        capacity = await self._available_capacity()
        if capacity <= 0:
            return

        size = min(self.settings.dialer.batch_size, capacity)
        batch: NextBatchResponse = await self.panel_client.get_next_batch(size=size)
        if not batch.call_allowed:
            retry = batch.retry_after_seconds or self.settings.dialer.default_retry
            self.next_panel_poll = now + timedelta(seconds=retry)
            logger.info("Panel disallowed calls; retry in %ss reason=%s", retry, batch.reason)
            return

        self.next_panel_poll = now + timedelta(seconds=60)
        if batch.numbers:
            await self._queue_panel_numbers(batch.numbers, batch.batch_id)

    async def _queue_panel_numbers(self, numbers: List[PanelNumber], batch_id: Optional[str]) -> None:
        items = [ContactItem(phone_number=n.phone_number, number_id=n.id, batch_id=batch_id) for n in numbers]
        async with self.lock:
            self.contacts.extend(items)
        logger.info("Queued %d contacts from panel batch %s", len(items), batch_id)

    async def _available_capacity(self) -> int:
        current_sessions = await self.session_manager.active_sessions_count()
        remaining_concurrency = max(self.settings.dialer.max_concurrent_calls - current_sessions, 0)
        self._prune_attempts()
        remaining_per_minute = max(self.settings.dialer.max_calls_per_minute - len(self.attempt_timestamps), 0)
        remaining_daily = max(self.settings.dialer.max_calls_per_day - self.daily_counter, 0)
        return min(remaining_concurrency, remaining_per_minute, remaining_daily)
