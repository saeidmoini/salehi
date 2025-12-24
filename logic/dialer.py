import asyncio
import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Deque, List, Optional

from config.settings import Settings
from core.ari_client import AriClient
from integrations.panel.client import NextBatchResponse, PanelClient, PanelNumber
from integrations.sms.melipayamak import SMSClient
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
        self.line_stats = {}
        for num in settings.dialer.outbound_numbers:
            norm = self._normalize_number(num)
            if not norm:
                continue
            self.line_stats[norm] = {
                "active": 0,
                "inbound_active": 0,
                "attempts": deque(),
                "daily": 0,
                "daily_marker": date.today(),
            }
        self.attempt_timestamps: Deque[datetime] = deque()  # global per-minute
        self.daily_counter = 0  # global per-day
        self.daily_marker: date = date.today()
        self._running = False
        self.lock = asyncio.Lock()
        self.last_originate_window_start: float = 0.0
        self.originate_count_in_window: int = 0
        self.next_panel_poll: datetime = datetime.utcnow()
        self.timeout_tasks: dict[str, asyncio.Task] = {}
        self.paused_by_failures = False
        self.failure_streak = 0
        self.sms_client = SMSClient(settings.sms) if settings.sms.api_key and settings.sms.sender else None
        self.paused_reason = ""
        self.session_line: dict[str, str] = {}
        self.inbound_session_line: dict[str, str] = {}
        self.waiting_inbound: dict[str, int] = {}

    async def run(self, stop_event: asyncio.Event) -> None:
        if self._running:
            return
        self._running = True
        logger.info("Dialer started with %d queued contacts", len(self.contacts))
        try:
            while not stop_event.is_set() and self._running:
                self._reset_daily_if_needed()
                await self._maybe_refill_from_panel()
                if self.paused_by_failures:
                    await asyncio.sleep(2)
                    continue
                if not await self._can_start_call():
                    await asyncio.sleep(1)
                    continue
                contact = await self._next_contact()
                if not contact:
                    await asyncio.sleep(5)
                    continue
                # Throttle to the configured originates per second (global across lines).
                rate_limit = self.settings.dialer.max_originations_per_second
                if rate_limit > 0:
                    now = asyncio.get_event_loop().time()
                    if now - self.last_originate_window_start >= 1.0:
                        self.last_originate_window_start = now
                        self.originate_count_in_window = 0
                    if self.originate_count_in_window >= rate_limit:
                        # Sleep until the next second window.
                        await asyncio.sleep(max(0, 1.0 - (now - self.last_originate_window_start)))
                        self.last_originate_window_start = asyncio.get_event_loop().time()
                        self.originate_count_in_window = 0
                    self.originate_count_in_window += 1
                await self._originate(contact)
                await asyncio.sleep(0.05)
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
        async with self.lock:
            line = self.session_line.pop(session_id, None)
            if line and line in self.line_stats:
                self.line_stats[line]["active"] = max(self.line_stats[line]["active"] - 1, 0)
            inbound_line = self.inbound_session_line.pop(session_id, None)
            if inbound_line and inbound_line in self.line_stats:
                stats = self.line_stats[inbound_line]
                stats["inbound_active"] = max(stats.get("inbound_active", 0) - 1, 0)
        # reset failure streak on completion unless paused
        if not self.paused_by_failures:
            self.failure_streak = 0

    async def register_inbound_session(self, session_id: str, line: str) -> bool:
        """
        Track inbound sessions per line so MAX_CONCURRENT_CALLS applies to combined inbound+outbound.
        Returns False when the line is already at or above capacity (caller should wait).
        """
        async with self.lock:
            stats = self.line_stats.get(line)
            if not stats:
                return True  # unknown line; do not block
            total_active = self._line_active_total(stats)
            if total_active >= self.settings.dialer.max_concurrent_calls:
                self.waiting_inbound[line] = self.waiting_inbound.get(line, 0) + 1
                return False
            stats["inbound_active"] = stats.get("inbound_active", 0) + 1
            self.inbound_session_line[session_id] = line
            return True

    async def try_register_waiting_inbound(self, session_id: str, line: str) -> bool:
        """
        Attempt to promote a waiting inbound call into an active slot.
        """
        async with self.lock:
            stats = self.line_stats.get(line)
            if not stats:
                return True
            total_active = self._line_active_total(stats)
            if total_active >= self.settings.dialer.max_concurrent_calls:
                return False
            stats["inbound_active"] = stats.get("inbound_active", 0) + 1
            self.inbound_session_line[session_id] = line
            if line in self.waiting_inbound:
                self.waiting_inbound[line] = max(0, self.waiting_inbound[line] - 1)
                if self.waiting_inbound[line] == 0:
                    del self.waiting_inbound[line]
            return True

    async def cancel_waiting_inbound(self, line: str) -> None:
        """
        Drop a waiting inbound marker when the caller hangs up before being served.
        """
        async with self.lock:
            if line in self.waiting_inbound:
                self.waiting_inbound[line] = max(0, self.waiting_inbound[line] - 1)
                if self.waiting_inbound[line] == 0:
                    del self.waiting_inbound[line]

    async def on_result(
        self,
        session_id: str,
        result: Optional[str],
        number_id: Optional[int],
        phone_number: Optional[str],
        batch_id: Optional[str],
        attempted_at_iso: Optional[str],
    ) -> None:
        if number_id is not None and result and result.startswith("failed"):
            self.failure_streak += 1
        else:
            self.failure_streak = 0
        if (
            self.failure_streak >= self.settings.sms.fail_alert_threshold
            and not self.paused_by_failures
        ):
            await self._handle_failure_threshold(
                session_id, result, number_id, phone_number, batch_id, attempted_at_iso
            )

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
        # Global guard: only proceed if some line is available
        line = self._available_line()
        return line is not None

    def _prune_attempts(self) -> None:
        cutoff = datetime.utcnow() - timedelta(minutes=1)
        while self.attempt_timestamps and self.attempt_timestamps[0] < cutoff:
            self.attempt_timestamps.popleft()

    def _prune_line_attempts(self, stats: dict) -> None:
        cutoff = datetime.utcnow() - timedelta(minutes=1)
        attempts: Deque[datetime] = stats.get("attempts", deque())
        while attempts and attempts[0] < cutoff:
            attempts.popleft()
        # reset daily if date changed
        today = date.today()
        if stats.get("daily_marker") != today:
            stats["daily_marker"] = today
            stats["daily"] = 0

    async def _next_contact(self) -> Optional[ContactItem]:
        async with self.lock:
            if not self.contacts:
                return None
            return self.contacts.popleft()

    async def _originate(self, contact: ContactItem) -> None:
        try:
            line = self._available_line()
            if not line:
                logger.info("No available outbound line for contact %s; requeueing", contact.phone_number)
                async with self.lock:
                    self.contacts.append(contact)
                await asyncio.sleep(1)
                return
            attempted_at = datetime.utcnow()
            contact.attempted_at = attempted_at
            metadata = {"attempted_at": attempted_at.isoformat()}
            if contact.number_id is not None:
                metadata["number_id"] = contact.number_id
            if contact.batch_id:
                metadata["batch_id"] = contact.batch_id
            metadata["outbound_line"] = line
            session = await self.session_manager.create_outbound_session(
                contact_number=contact.phone_number,
                metadata=metadata,
            )
            endpoint = self._build_endpoint(contact, line)
            app_args = f"outbound,{session.session_id}"
            await self.ari_client.originate_call(
                endpoint=endpoint,
                app_args=app_args,
                caller_id=self.settings.dialer.default_caller_id,
                timeout=self.settings.dialer.origination_timeout,
            )
            self._schedule_timeout_watch(session.session_id)
            async with self.lock:
                stats = self.line_stats.get(line)
                if stats:
                    stats["active"] += 1
                    stats["attempts"].append(datetime.utcnow())
                    stats["daily"] += 1
                if not hasattr(self, "session_line"):
                    self.session_line = {}
                self.session_line[session.session_id] = line
            self._record_attempt()
            logger.info(
                "Origination requested for %s (session %s) via line %s", contact.phone_number, session.session_id, line
            )
        except Exception as exc:
            logger.exception("Failed to originate call to %s: %s", contact.phone_number, exc)

    def _record_attempt(self) -> None:
        self.attempt_timestamps.append(datetime.utcnow())
        self.daily_counter += 1

    def _build_endpoint(self, contact: ContactItem, line: str) -> str:
        trunk = self.settings.dialer.outbound_trunk
        customer_digits = self._normalize_number(contact.phone_number) or contact.phone_number
        suffix = line[-4:] if len(line) >= 4 else line
        dial_str = f"{suffix}{customer_digits}"
        return f"PJSIP/{dial_str}@{trunk}"

    def _normalize_number(self, number: Optional[str]) -> Optional[str]:
        if not number:
            return None
        digits = "".join(ch for ch in number if ch.isdigit())
        return digits or None

    def _line_active_total(self, stats: dict) -> int:
        return stats.get("active", 0) + stats.get("inbound_active", 0)

    def _available_line(self) -> Optional[str]:
        now = datetime.utcnow()
        best = None
        best_load = None
        for line, stats in self.line_stats.items():
            if not line:
                continue
            self._prune_line_attempts(stats)
            if self.waiting_inbound.get(line, 0) > 0:
                # Hold outbound when inbound callers are waiting for this line.
                continue
            total_active = self._line_active_total(stats)
            if total_active >= self.settings.dialer.max_concurrent_calls:
                continue
            if len(stats["attempts"]) >= self.settings.dialer.max_calls_per_minute:
                continue
            if stats["daily"] >= self.settings.dialer.max_calls_per_day:
                continue
            load = (total_active, len(stats["attempts"]), stats["daily"])
            if best_load is None or load < best_load:
                best = line
                best_load = load
        return best

    async def _handle_failure_threshold(
        self,
        session_id: str,
        result: Optional[str],
        number_id: Optional[int],
        phone_number: Optional[str],
        batch_id: Optional[str],
        attempted_at_iso: Optional[str],
    ) -> None:
        self.paused_by_failures = True
        self.paused_reason = "consecutive_failures"
        msg = f"Dialer paused after {self.failure_streak} consecutive FAILURES. Last result={result}"
        logger.error(msg)
        if self.sms_client:
            try:
                await self.sms_client.send_message(
                    text=msg,
                    to_override=None,
                )
            except Exception as exc:
                logger.warning("SMS alert send failed: %s", exc)
        # Inform panel to pause
        if self.panel_client and (number_id is not None or phone_number):
            from datetime import datetime, timezone

            attempted_at = datetime.utcnow().replace(tzinfo=timezone.utc)
            if attempted_at_iso:
                try:
                    attempted_at = datetime.fromisoformat(attempted_at_iso)
                    if not attempted_at.tzinfo:
                        attempted_at = attempted_at.replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            try:
                await self.panel_client.report_result(
                    number_id=number_id,
                    phone_number=phone_number,
                    status="FAILED",
                    reason=result or "failed",
                    attempted_at=attempted_at,
                    batch_id=batch_id,
                    call_allowed=False,
                )
            except Exception as exc:
                logger.warning("Failed to notify panel to pause after failures: %s", exc)

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
                # If the call is already active/answered or completed, do nothing.
                if session.status in {SessionStatus.ACTIVE, SessionStatus.COMPLETED}:
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
        if batch.call_allowed and self.paused_by_failures:
            logger.info("Panel re-enabled; resuming dialer after failures.")
            self.paused_by_failures = False
            self.failure_streak = 0
            self.paused_reason = ""
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
        available_slots = 0
        outbound_active_total = 0
        for line, stats in self.line_stats.items():
            if not line:
                continue
            self._prune_line_attempts(stats)
            if self.waiting_inbound.get(line, 0) > 0:
                continue
            total_active = self._line_active_total(stats)
            remaining_concurrency = self.settings.dialer.max_concurrent_calls - total_active
            remaining_per_minute = self.settings.dialer.max_calls_per_minute - len(stats["attempts"])
            remaining_daily = self.settings.dialer.max_calls_per_day - stats["daily"]
            line_slots = min(remaining_concurrency, remaining_per_minute, remaining_daily)
            if line_slots > 0:
                available_slots += line_slots
            outbound_active_total += stats["active"]

        # Optional global outbound cap: only apply if >0.
        if self.settings.dialer.max_concurrent_outbound_calls > 0:
            remaining_outbound = self.settings.dialer.max_concurrent_outbound_calls - outbound_active_total
            available_slots = min(available_slots, max(0, remaining_outbound))
        return max(0, available_slots)
