import asyncio
import logging
import uuid
from typing import Dict, Optional

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

    def __init__(self, ari_client: AriClient, scenario_handler):
        self.ari_client = ari_client
        self.scenario_handler = scenario_handler
        self.sessions: Dict[str, Session] = {}
        self.channel_to_session: Dict[str, str] = {}
        self.playback_to_session: Dict[str, str] = {}
        self.recording_to_session: Dict[str, str] = {}
        self.lock = asyncio.Lock()

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
                session = Session(session_id=session_id)
                async with self.lock:
                    self.sessions[session_id] = session
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
            session = Session(session_id=session_id)
            async with session.lock:
                session.inbound_leg = CallLeg(
                    channel_id=channel_id,
                    direction=direction,
                    endpoint=channel.get("caller", {}).get("number", "unknown"),
                )
                session.status = SessionStatus.RINGING
            async with self.lock:
                self.sessions[session_id] = session
            await self._index_channel(session_id, channel_id)
            await self._ensure_bridge(session)
            if session.bridge and channel_id:
                await self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            # Auto-answer inbound leg to run the same scenario as outbound.
            try:
                await self.ari_client.answer_channel(channel_id)
            except Exception as exc:
                logger.warning("Failed to answer inbound channel %s: %s", channel_id, exc)
            await self._maybe_mark_answered(session, session.inbound_leg, channel_state)
            if self.scenario_handler:
                await self.scenario_handler.on_inbound_channel_created(session)
            logger.info("Inbound channel %s created session %s", channel_id, session_id)
            # Capture forwarding headers for inbound to know the intermediate line.
            divert = await self.ari_client.get_channel_variable(channel_id, "SIP_HEADER(Diversion)")
            pai = await self.ari_client.get_channel_variable(channel_id, "SIP_HEADER(P-Asserted-Identity)")
            if divert or pai:
                logger.info(
                    "Inbound forward info session=%s diversion=%s p_asserted=%s",
                    session.session_id,
                    divert,
                    pai,
                )
                async with session.lock:
                    if divert:
                        session.metadata["diversion"] = divert
                    if pai:
                        session.metadata["p_asserted_identity"] = pai

    async def _handle_channel_state_change(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_state = channel.get("state")
        session = await self._get_session_by_channel(channel_id)
        if not session:
            logger.debug("Channel state change for unknown channel %s", channel_id)
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

    async def _handle_hangup(self, event: dict) -> None:
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

        if session.bridge:
            try:
                await self.ari_client.delete_bridge(session.bridge.bridge_id)
            except Exception as exc:
                logger.warning(
                    "Failed to delete bridge %s for session %s: %s",
                    session.bridge.bridge_id,
                    session.session_id,
                    exc,
                )
        logger.info("Cleaned session %s", session.session_id)

    async def active_sessions_count(self) -> int:
        async with self.lock:
            return len(self.sessions)

    def _detect_direction(self, args: list) -> LegDirection:
        if not args:
            return LegDirection.INBOUND
        if args[0] == "outbound":
            return LegDirection.OUTBOUND
        if args[0] == "operator":
            return LegDirection.OPERATOR
        return LegDirection.INBOUND

    def _find_leg(self, session: Session, channel_id: str) -> Optional[CallLeg]:
        for leg in (session.inbound_leg, session.outbound_leg, session.operator_leg):
            if leg and leg.channel_id == channel_id:
                return leg
        return None

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
