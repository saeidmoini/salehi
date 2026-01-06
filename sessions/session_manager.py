import asyncio
import logging
import time
import uuid
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

from core.ari_client import AriClient
from sessions.session import (
    BridgeInfo,
    CallLeg,
    LegDirection,
    LegState,
    Session,
    SessionStatus,
)


logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages sessions, bridges, and routing of ARI events into scenario logic.
    """

    def __init__(
        self,
        ari_client: AriClient,
        scenario_handler,
        allowed_inbound_numbers: Optional[list[str]] = None,
        max_inbound_calls: Optional[int] = None,
    ):
        self.ari_client = ari_client
        self.scenario_handler = scenario_handler
        self.sessions: Dict[str, Session] = {}
        self.channel_to_session: Dict[str, str] = {}
        self.playback_to_session: Dict[str, str] = {}
        self.recording_to_session: Dict[str, str] = {}
        self.lock = asyncio.Lock()
        # Inbound is allowed for all; we keep the set for mapping/priority.
        self.inbound_lines = [self._normalize_number(n) for n in (allowed_inbound_numbers or []) if n]
        self.allowed_inbound_numbers = {norm for norm in self.inbound_lines if norm}
        self.max_inbound_calls = max_inbound_calls
        self.hangup_logger = logging.getLogger("sessions.hangups")
        self.userdrop_logger = logging.getLogger("sessions.userdrop")
        self._ensure_hangup_log_handler()
        self.dialer = None
        self.waiting_inbound: Dict[str, Deque[Tuple[str, str]]] = {}

    def _ensure_hangup_log_handler(self) -> None:
        # Add a dedicated rolling log for hangup tracing if not already present.
        for handler in self.hangup_logger.handlers:
            if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "").endswith("hangups.log"):
                return
        log_dir = Path("logs")
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_dir / "hangups.log", maxBytes=2 * 1024 * 1024, backupCount=3)
        formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
        handler.setFormatter(formatter)
        self.hangup_logger.addHandler(handler)
        # Keep propagate=True so it still hits the main app.log.

        # Dedicated log for user/disconnected timing analysis.
        for handler in self.userdrop_logger.handlers:
            if isinstance(handler, RotatingFileHandler) and getattr(handler, "baseFilename", "").endswith("userdrop.log"):
                return
        user_handler = RotatingFileHandler(log_dir / "userdrop.log", maxBytes=2 * 1024 * 1024, backupCount=3)
        user_handler.setFormatter(formatter)
        self.userdrop_logger.addHandler(user_handler)

    async def create_outbound_session(
        self, contact_number: str, metadata: Optional[Dict[str, str]] = None
    ) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(session_id=session_id, metadata={"contact_number": contact_number})
        if metadata:
            session.metadata.update(metadata)
        async with self.lock:
            self.sessions[session_id] = session
        logger.info("Created outbound session %s for %s", session_id, contact_number)
        return session

    async def get_session(self, session_id: str) -> Optional[Session]:
        async with self.lock:
            return self.sessions.get(session_id)

    async def _index_channel(self, session_id: str, channel_id: str) -> None:
        async with self.lock:
            self.channel_to_session[channel_id] = session_id

    async def _get_session_by_channel(self, channel_id: str) -> Optional[Session]:
        async with self.lock:
            session_id = self.channel_to_session.get(channel_id)
            if session_id:
                return self.sessions.get(session_id)
        return None

    async def handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if not event_type:
            return

        if event_type == "StasisStart":
            await self._handle_stasis_start(event)
        elif event_type == "ChannelStateChange":
            await self._handle_channel_state_change(event)
        elif event_type == "ChannelHangupRequest":
            if not (event.get("channel") or {}).get("id") in self.channel_to_session:
                logger.debug("HangupRequest before session map: %s", event)
            await self._handle_hangup(event)
        elif event_type == "ChannelDestroyed":
            await self._handle_channel_destroyed(event)
        elif event_type == "PlaybackStarted":
            await self._handle_playback_started(event)
        elif event_type == "PlaybackFinished":
            await self._handle_playback_finished(event)
        elif event_type == "RecordingFinished":
            await self._handle_recording_finished(event)
        elif event_type == "RecordingFailed":
            await self._handle_recording_failed(event)
        elif event_type == "StasisEnd":
            await self._handle_stasis_end(event)
        elif event_type == "Dial":
            # Visibility into pre-Stasis dial failures (cause/dialstatus may appear here).
            logger.info("Dial event: %s", event)
        else:
            logger.debug("Unhandled event type: %s", event_type)

    async def _ensure_bridge(self, session: Session) -> None:
        async with session.lock:
            if session.bridge:
                return
            bridge = await self.ari_client.create_bridge(name=f"session-{session.session_id}")
            session.bridge = BridgeInfo(
                bridge_id=bridge.get("id"),
                bridge_type=bridge.get("bridge_type", "mixing"),
            )
        logger.info("Bridge %s created for session %s", session.bridge.bridge_id, session.session_id)

    async def _handle_stasis_start(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_state = channel.get("state")
        args = event.get("args", [])
        direction = self._detect_direction(args)

        if direction == LegDirection.OUTBOUND and len(args) >= 2:
            session_id = args[1]
            session = await self.get_session(session_id)
            if not session:
                session = Session(session_id=session_id)
                async with self.lock:
                    self.sessions[session_id] = session
            async with session.lock:
                session.outbound_leg = CallLeg(
                    channel_id=channel_id,
                    direction=direction,
                    endpoint=session.metadata.get("contact_number", "unknown"),
                )
                session.status = SessionStatus.RINGING
            await self._index_channel(session_id, channel_id)
            await self._ensure_bridge(session)
            if session.bridge and channel_id:
                await self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            await self._maybe_mark_answered(session, session.outbound_leg, channel_state)
            if self.scenario_handler:
                await self.scenario_handler.on_outbound_channel_created(session)
            logger.info(
                "Outbound channel %s joined session %s", channel_id, session.session_id
            )
        elif direction == LegDirection.OPERATOR and len(args) >= 2:
            session_id = args[1]
            endpoint = args[2] if len(args) >= 3 else "operator"
            session = await self.get_session(session_id)
            if not session:
                # Customer leg is already gone; tear down this orphan operator leg.
                logger.info("Operator leg %s has no session %s; hanging up", channel_id, session_id)
                try:
                    await self.ari_client.hangup_channel(channel_id)
                except Exception as exc:
                    logger.debug("Failed to hangup orphan operator leg %s: %s", channel_id, exc)
                return
            async with session.lock:
                session.operator_leg = CallLeg(
                    channel_id=channel_id,
                    direction=direction,
                    endpoint=endpoint,
                )
                session.status = SessionStatus.RINGING
            await self._index_channel(session_id, channel_id)
            await self._ensure_bridge(session)
            if session.bridge and channel_id:
                await self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            await self._maybe_mark_answered(session, session.operator_leg, channel_state)
            if self.scenario_handler and hasattr(self.scenario_handler, "on_operator_channel_created"):
                await self.scenario_handler.on_operator_channel_created(session)
            logger.info(
                "Operator channel %s joined session %s", channel_id, session.session_id
            )
        else:
            # Inbound calls are keyed by their channel id for the session id.
            session_id = channel_id
            inbound_line = self._detect_inbound_line(channel)
            # Enforce inbound concurrency limit only if a positive limit is set.
            if self.max_inbound_calls is not None and self.max_inbound_calls > 0:
                active_inbound = await self.inbound_active_count()
                if active_inbound >= self.max_inbound_calls:
                    logger.warning(
                        "Inbound concurrency limit reached (%s); rejecting channel %s",
                        self.max_inbound_calls,
                        channel_id,
                    )
                    try:
                        await self.ari_client.hangup_channel(channel_id)
                    except Exception:
                        pass
                    return
            waiting_for_slot = False
            if inbound_line and self.dialer:
                reserved = await self.dialer.register_inbound_session(session_id, inbound_line)
                waiting_for_slot = not reserved
                if waiting_for_slot:
                    logger.info(
                        "Inbound channel %s queued for line %s (capacity full); will answer when free",
                        channel_id,
                        inbound_line,
                    )
            session = Session(session_id=session_id)
            async with session.lock:
                session.inbound_leg = CallLeg(
                    channel_id=channel_id,
                    direction=direction,
                    endpoint=channel.get("caller", {}).get("number", "unknown"),
                )
                session.status = SessionStatus.RINGING if not waiting_for_slot else SessionStatus.INITIATING
                caller_num = channel.get("caller", {}).get("number")
                if caller_num:
                    session.metadata["caller_number"] = caller_num
                    session.metadata["contact_number"] = caller_num
                called_num = channel.get("connected", {}).get("number") or channel.get("dialplan", {}).get("exten")
                divert_header = None
                if inbound_line:
                    session.metadata["inbound_line"] = inbound_line
                if waiting_for_slot:
                    session.metadata["inbound_waiting"] = "1"
                caller_num = session.metadata.get("caller_number")
            async with self.lock:
                self.sessions[session_id] = session
            await self._update_contact_number(session, caller_num)
            await self._index_channel(session_id, channel_id)
            await self._ensure_bridge(session)
            if session.bridge and channel_id:
                await self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            if waiting_for_slot:
                await self._queue_waiting_inbound(inbound_line, session_id, channel_id)
                return
            await self._accept_inbound(session, channel_id, channel_state)

    async def _handle_channel_state_change(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_state = channel.get("state")
        session = await self._get_session_by_channel(channel_id)
        if not session:
            logger.info("Channel state change for unknown channel %s payload=%s", channel_id, event)
            return

        leg = self._find_leg(session, channel_id)
        if leg is None:
            logger.debug("No leg mapped for channel %s", channel_id)
            return

        async with session.lock:
            if channel_state == "Up":
                leg.state = LegState.ANSWERED
                session.status = SessionStatus.ACTIVE
            elif channel_state == "Ringing":
                leg.state = LegState.RINGING
            elif channel_state in {"Busy", "Failed"}:
                leg.state = LegState.FAILED
                session.status = SessionStatus.FAILED
                if leg.direction == LegDirection.OPERATOR:
                    session.result = session.result or "failed:operator_failed"

        if channel_state == "Up" and self.scenario_handler:
            await self.scenario_handler.on_call_answered(session, leg)
        elif channel_state in {"Busy", "Failed"} and self.scenario_handler:
            await self.scenario_handler.on_call_failed(session, reason=channel_state)
        else:
            # Detect early busy/congestion signals so we don't wait for timeout.
            # PJSIP may send Progress (183) with Reason cause=17/34/41/42 or text.
            cause_raw = event.get("cause") or channel.get("cause")
            cause = str(cause_raw) if cause_raw is not None else None
            cause_txt = event.get("cause_txt") or channel.get("cause_txt")
            busy_like = {"17", "18", "19", "20", "21", "34", "41", "42"}
            if (
                (cause in busy_like)
                or (cause_raw in {17, 18, 19, 20, 21, 34, 41, 42})
                or (cause_txt and any(x in cause_txt.lower() for x in ["busy", "congest"]))
            ):
                if self.scenario_handler:
                    await self.scenario_handler.on_call_failed(session, reason=cause_txt or cause)

    async def _handle_hangup(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        # ARI sometimes provides cause/cause_txt on the event or on the channel payload.
        cause = event.get("cause") or channel.get("cause")
        cause_txt = event.get("cause_txt") or channel.get("cause_txt")
        session = await self._get_session_by_channel(channel_id)
        if not session:
            return
        leg = self._find_leg(session, channel_id)
        async with session.lock:
            if leg:
                leg.state = LegState.HUNGUP
            session.status = SessionStatus.COMPLETED
            if cause:
                session.metadata["hangup_cause"] = str(cause)
            if cause_txt:
                session.metadata["hangup_cause_txt"] = cause_txt
            if leg:
                session.metadata["hungup_by"] = leg.direction.value
        self.hangup_logger.info(
            "Hangup session=%s contact=%s channel=%s leg=%s cause=%s cause_txt=%s result=%s",
            session.session_id,
            session.metadata.get("contact_number"),
            channel_id,
            leg.direction.value if leg else "unknown",
            cause,
            cause_txt,
            session.result,
        )
        # Detailed timing for customer leg hangups (user drops / disconnects).
        if leg and leg.direction == LegDirection.OUTBOUND:
            now = time.time()
            answered_at = float(session.metadata.get("answered_at", "0") or 0)
            yes_at = float(session.metadata.get("yes_at", "0") or 0)
            t_answer_to_hang = now - answered_at if answered_at else None
            t_yes_to_hang = now - yes_at if yes_at else None
            self.userdrop_logger.info(
                "UserDrop session=%s contact=%s result=%s cause=%s cause_txt=%s t_answer_to_hang=%s t_yes_to_hang=%s",
                session.session_id,
                session.metadata.get("contact_number"),
                session.result,
                cause,
                cause_txt,
                f"{t_answer_to_hang:.3f}" if t_answer_to_hang is not None else "na",
                f"{t_yes_to_hang:.3f}" if t_yes_to_hang is not None else "na",
            )
        # If we have a clear failure cause (busy/congest/power-off/banned), notify scenario before hangup finish.
        busy_like = {"17", "18", "19", "20", "21", "34", "41", "42"}
        if self.scenario_handler and (
            (cause and (str(cause) in busy_like or cause in {17, 18, 19, 20, 21, 34, 41, 42}))
            or (cause_txt and any(x in cause_txt.lower() for x in ["busy", "congest"]))
        ):
            try:
                await self.scenario_handler.on_call_failed(
                    session, reason=(cause_txt or (str(cause) if cause is not None else None))
                )
            except Exception as exc:  # best-effort; don't block cleanup
                logger.debug("on_call_failed during hangup failed for %s: %s", session.session_id, exc)

        if self.scenario_handler:
            await self.scenario_handler.on_call_hangup(session)
        await self._cleanup_session(session)

    async def _handle_channel_destroyed(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = await self._get_session_by_channel(channel_id)
        if not session:
            return
        leg = self._find_leg(session, channel_id)
        async with session.lock:
            if leg:
                leg.state = LegState.HUNGUP
            session.status = SessionStatus.COMPLETED
        await self._cleanup_session(session)

    async def _handle_playback_finished(self, event: dict) -> None:
        playback = event.get("playback", {})
        playback_id = playback.get("id")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = await self._get_session_by_channel(channel_id) if channel_id else None
        if not session and playback_id:
            session = await self._get_session_by_playback(playback_id)
        if not session or not self.scenario_handler:
            return
        await self.scenario_handler.on_playback_finished(session, playback_id)

    async def _handle_playback_started(self, event: dict) -> None:
        playback = event.get("playback", {})
        playback_id = playback.get("id")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if playback_id and channel_id:
            async with self.lock:
                if playback_id not in self.playback_to_session:
                    session_id = self.channel_to_session.get(channel_id)
                    if session_id:
                        self.playback_to_session[playback_id] = session_id

    async def _handle_stasis_end(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = await self._get_session_by_channel(channel_id)
        if session:
            await self._cleanup_session(session)

    async def _cleanup_session(self, session: Session) -> None:
        # Prevent duplicate cleanup
        async with session.lock:
            if session.metadata.get("cleanup_done") == "1":
                return
            session.metadata["cleanup_done"] = "1"

        report = False
        if self.scenario_handler:
            async with session.lock:
                if session.metadata.get("finished_reported") != "1":
                    session.metadata["finished_reported"] = "1"
                    report = True
        if report:
            try:
                await self.scenario_handler.on_call_finished(session)
            except Exception as exc:
                logger.exception("Error reporting call finished for session %s: %s", session.session_id, exc)

        # Proactively hang up any remaining legs before cleaning.
        tasks = []
        for leg in (session.inbound_leg, session.outbound_leg, session.operator_leg):
            if leg and leg.channel_id and leg.state not in {LegState.HUNGUP, LegState.FAILED}:
                tasks.append(self.ari_client.hangup_channel(leg.channel_id))
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        async with self.lock:
            for channel_id, session_id in list(self.channel_to_session.items()):
                if session_id == session.session_id:
                    del self.channel_to_session[channel_id]
            for playback_id, session_id in list(self.playback_to_session.items()):
                if session_id == session.session_id:
                    del self.playback_to_session[playback_id]
            for recording_name, session_id in list(self.recording_to_session.items()):
                if session_id == session.session_id:
                    del self.recording_to_session[recording_name]
            self.sessions.pop(session.session_id, None)

        # If this session was waiting for capacity, clear its marker.
        waiting_line = await self._remove_from_waiting(session.session_id)
        if waiting_line and self.dialer:
            await self.dialer.cancel_waiting_inbound(waiting_line)

        if session.bridge:
            try:
                await self.ari_client.delete_bridge(session.bridge.bridge_id)
            except Exception as exc:
                msg = (
                    "Failed to delete bridge %s for session %s: %s",
                    session.bridge.bridge_id,
                    session.session_id,
                    exc,
                )
                if "404" in str(exc):
                    logger.debug(*msg)
                else:
                    logger.warning(*msg)
        if self.dialer:
            try:
                await self.dialer.on_session_completed(session.session_id)
            except Exception as exc:
                logger.debug(
                    "Failed to notify dialer of session cleanup for %s: %s",
                    session.session_id,
                    exc,
                )
        inbound_line = session.metadata.get("inbound_line") or session.metadata.get("outbound_line")
        if inbound_line:
            await self._try_start_waiting_inbound(inbound_line)
        logger.info("Cleaned session %s", session.session_id)

    async def active_sessions_count(self) -> int:
        async with self.lock:
            return len(self.sessions)

    async def inbound_active_count(self) -> int:
        """
        Count inbound sessions that are still ringing or active.
        Used to share concurrency limits with outbound calls.
        """
        async with self.lock:
            sessions = list(self.sessions.values())
        active_states = {SessionStatus.RINGING, SessionStatus.ACTIVE}
        return sum(
            1
            for s in sessions
            if s.inbound_leg is not None
            and s.status in active_states
            and s.metadata.get("inbound_waiting") != "1"
        )

    def _detect_direction(self, args: list) -> LegDirection:
        if not args:
            return LegDirection.INBOUND
        if args[0] == "outbound":
            return LegDirection.OUTBOUND
        if args[0] == "operator":
            return LegDirection.OPERATOR
        return LegDirection.INBOUND

    def attach_dialer(self, dialer) -> None:
        """
        Provide dialer access so inbound calls can share per-line concurrency with outbound.
        """
        self.dialer = dialer

    def _detect_inbound_line(self, channel: dict) -> Optional[str]:
        """
        Attempt to map an inbound channel to a configured line number.
        """
        candidates = [
            channel.get("connected", {}).get("number"),
            channel.get("dialplan", {}).get("exten"),
        ]
        for raw in candidates:
            norm = self._normalize_number(raw)
            if not norm:
                continue
            match = self._match_line_number(norm)
            if match:
                return match
        return None

    async def _accept_inbound(self, session: Session, channel_id: str, channel_state: Optional[str]) -> None:
        # Auto-answer inbound leg to run the same scenario as outbound.
        try:
            await self.ari_client.answer_channel(channel_id)
        except Exception as exc:
            logger.warning("Failed to answer inbound channel %s: %s", channel_id, exc)
        await self._maybe_mark_answered(session, session.inbound_leg, channel_state)
        if self.scenario_handler:
            await self.scenario_handler.on_inbound_channel_created(session)
        divert = await self._get_header(channel_id, "Diversion")
        pai = await self._get_header(channel_id, "P-Asserted-Identity")
        await self._update_contact_number(session, pai, divert)
        logger.info(
            "Inbound channel %s created session %s caller=%s diversion=%s p_asserted=%s",
            channel_id,
            session.session_id,
            session.metadata.get("caller_number"),
            divert,
            pai,
        )
        if divert or pai:
            async with session.lock:
                if divert:
                    session.metadata["diversion"] = divert
                if pai:
                    session.metadata["p_asserted_identity"] = pai

    async def _queue_waiting_inbound(self, line: str, session_id: str, channel_id: str) -> None:
        async with self.lock:
            queue = self.waiting_inbound.setdefault(line, deque())
            queue.append((session_id, channel_id))

    async def _get_header(self, channel_id: str, name: str) -> Optional[str]:
        """
        Read a SIP header using the PJSIP header function; suppress errors if not available.
        """
        return await self.ari_client.get_channel_variable(
            channel_id, f"PJSIP_HEADER(read,{name})"
        )

    async def _remove_from_waiting(self, session_id: str) -> Optional[str]:
        async with self.lock:
            for line, queue in list(self.waiting_inbound.items()):
                for sid, ch_id in list(queue):
                    if sid == session_id:
                        try:
                            queue.remove((sid, ch_id))
                        except ValueError:
                            pass
                        if not queue:
                            del self.waiting_inbound[line]
                        return line
        return None

    async def _try_start_waiting_inbound(self, line: str) -> None:
        if not self.dialer:
            return
        while True:
            async with self.lock:
                queue = self.waiting_inbound.get(line)
                item: Optional[Tuple[str, str]] = queue[0] if queue else None
            if not item:
                return
            session_id, channel_id = item
            promoted = await self.dialer.try_register_waiting_inbound(session_id, line)
            if not promoted:
                # Still full; keep waiting.
                return
            async with self.lock:
                queue = self.waiting_inbound.get(line)
                if queue and queue and queue[0][0] == session_id:
                    queue.popleft()
                    if not queue:
                        del self.waiting_inbound[line]
            session = await self.get_session(session_id)
            if not session:
                # Session gone; try next if any.
                await self.dialer.on_session_completed(session_id)
                continue
            async with session.lock:
                session.metadata.pop("inbound_waiting", None)
                session.status = SessionStatus.RINGING
            await self._accept_inbound(session, channel_id, None)
            return

    def _match_line_number(self, norm_candidate: str) -> Optional[str]:
        """
        Match an inbound dialed/connected number to a configured outbound line.
        Accepts exact match, leading-zero trimmed match, or suffix match.
        """
        if not self.inbound_lines:
            return None
        trimmed_candidate = norm_candidate.lstrip("0")
        for line in self.inbound_lines:
            if not line:
                continue
            if norm_candidate == line:
                return line
            trimmed_line = line.lstrip("0")
            if trimmed_candidate and trimmed_line and trimmed_candidate == trimmed_line:
                return line
            if trimmed_candidate and trimmed_line and trimmed_candidate.endswith(trimmed_line):
                return line
        return None

    async def _update_contact_number(self, session: Session, *candidates: Optional[str]) -> None:
        """
        Normalize and set the contact number, preferring versions with leading 0.
        """
        current_raw = None
        normalized_current = None
        async with session.lock:
            current_raw = session.metadata.get("contact_number")
            normalized_current = self._normalize_contact_number(current_raw)
        for cand in candidates:
            norm = self._normalize_contact_number(cand)
            if not norm:
                continue
            if normalized_current:
                if (
                    not normalized_current.startswith("0")
                    and norm.startswith("0")
                    and norm.endswith(normalized_current)
                ):
                    normalized_current = norm
                break
            normalized_current = norm
            break
        if normalized_current and normalized_current != current_raw:
            async with session.lock:
                session.metadata["contact_number"] = normalized_current

    def _normalize_contact_number(self, value: Optional[str]) -> Optional[str]:
        digits = self._normalize_number(value)
        if not digits:
            return None
        if len(digits) == 10 and not digits.startswith("0"):
            return f"0{digits}"
        return digits

    def _find_leg(self, session: Session, channel_id: str) -> Optional[CallLeg]:
        for leg in (session.inbound_leg, session.outbound_leg, session.operator_leg):
            if leg and leg.channel_id == channel_id:
                return leg
        return None

    @staticmethod
    def _normalize_number(number: Optional[str]) -> Optional[str]:
        if not number:
            return None
        digits = "".join(ch for ch in number if ch.isdigit())
        return digits or None

    @staticmethod
    def _extract_number_from_header(header: Optional[str]) -> Optional[str]:
        if not header:
            return None
        # crude parse: find digits in header
        return SessionManager._normalize_number(header)

    async def _maybe_mark_answered(self, session: Session, leg: CallLeg, channel_state: Optional[str]) -> None:
        if channel_state == "Up":
            async with session.lock:
                leg.state = LegState.ANSWERED
                session.status = SessionStatus.ACTIVE
            if self.scenario_handler:
                await self.scenario_handler.on_call_answered(session, leg)

    async def register_playback(self, session_id: str, playback_id: str) -> None:
        async with self.lock:
            self.playback_to_session[playback_id] = session_id

    async def _get_session_by_playback(self, playback_id: str) -> Optional[Session]:
        async with self.lock:
            session_id = self.playback_to_session.get(playback_id)
            if session_id:
                return self.sessions.get(session_id)
        return None

    async def register_recording(self, session_id: str, recording_name: str) -> None:
        async with self.lock:
            self.recording_to_session[recording_name] = session_id

    async def _get_session_by_recording(self, recording_name: str) -> Optional[Session]:
        async with self.lock:
            session_id = self.recording_to_session.get(recording_name)
            if session_id:
                return self.sessions.get(session_id)
        return None

    async def _handle_recording_finished(self, event: dict) -> None:
        recording = event.get("recording", {})
        recording_name = recording.get("name")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = await self._get_session_by_channel(channel_id) if channel_id else None
        if not session and recording_name:
            session = await self._get_session_by_recording(recording_name)
        if session and self.scenario_handler and hasattr(self.scenario_handler, "on_recording_finished"):
            await self.scenario_handler.on_recording_finished(session, recording_name)

    async def _handle_recording_failed(self, event: dict) -> None:
        recording = event.get("recording", {})
        recording_name = recording.get("name")
        cause = recording.get("cause", "unknown")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = await self._get_session_by_channel(channel_id) if channel_id else None
        if not session and recording_name:
            session = await self._get_session_by_recording(recording_name)
        if session and self.scenario_handler and hasattr(self.scenario_handler, "on_recording_failed"):
            await self.scenario_handler.on_recording_failed(session, recording_name, cause)
