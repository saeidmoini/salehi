import logging
import threading
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
        self.lock = threading.Lock()

    def create_outbound_session(
        self, contact_number: str, metadata: Optional[Dict[str, str]] = None
    ) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(session_id=session_id, metadata={"contact_number": contact_number})
        if metadata:
            session.metadata.update(metadata)
        with self.lock:
            self.sessions[session_id] = session
        logger.info("Created outbound session %s for %s", session_id, contact_number)
        return session

    def get_session(self, session_id: str) -> Optional[Session]:
        with self.lock:
            return self.sessions.get(session_id)

    def _index_channel(self, session_id: str, channel_id: str) -> None:
        with self.lock:
            self.channel_to_session[channel_id] = session_id

    def _get_session_by_channel(self, channel_id: str) -> Optional[Session]:
        with self.lock:
            session_id = self.channel_to_session.get(channel_id)
        if session_id:
            return self.get_session(session_id)
        return None

    def handle_event(self, event: dict) -> None:
        event_type = event.get("type")
        if not event_type:
            return

        if event_type == "StasisStart":
            self._handle_stasis_start(event)
        elif event_type == "ChannelStateChange":
            self._handle_channel_state_change(event)
        elif event_type == "ChannelHangupRequest":
            self._handle_hangup(event)
        elif event_type == "ChannelDestroyed":
            self._handle_channel_destroyed(event)
        elif event_type == "PlaybackStarted":
            self._handle_playback_started(event)
        elif event_type == "PlaybackFinished":
            self._handle_playback_finished(event)
        elif event_type == "StasisEnd":
            self._handle_stasis_end(event)
        else:
            logger.debug("Unhandled event type: %s", event_type)

    def _ensure_bridge(self, session: Session) -> None:
        if session.bridge:
            return
        bridge = self.ari_client.create_bridge(name=f"session-{session.session_id}")
        session.bridge = BridgeInfo(
            bridge_id=bridge.get("id"),
            bridge_type=bridge.get("bridge_type", "mixing"),
        )
        logger.info("Bridge %s created for session %s", session.bridge.bridge_id, session.session_id)

    def _handle_stasis_start(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_state = channel.get("state")
        args = event.get("args", [])
        direction = self._detect_direction(args)

        if direction == LegDirection.OUTBOUND and len(args) >= 2:
            session_id = args[1]
            session = self.get_session(session_id)
            if not session:
                session = Session(session_id=session_id)
                with self.lock:
                    self.sessions[session_id] = session
            session.outbound_leg = CallLeg(
                channel_id=channel_id,
                direction=direction,
                endpoint=session.metadata.get("contact_number", "unknown"),
            )
            session.status = SessionStatus.RINGING
            self._index_channel(session_id, channel_id)
            self._ensure_bridge(session)
            if session.bridge and channel_id:
                self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            self._maybe_mark_answered(session, session.outbound_leg, channel_state)
            self.scenario_handler.on_outbound_channel_created(session)
            logger.info(
                "Outbound channel %s joined session %s", channel_id, session.session_id
            )
        elif direction == LegDirection.OPERATOR and len(args) >= 2:
            session_id = args[1]
            endpoint = args[2] if len(args) >= 3 else "operator"
            session = self.get_session(session_id)
            if not session:
                session = Session(session_id=session_id)
                with self.lock:
                    self.sessions[session_id] = session
            session.operator_leg = CallLeg(
                channel_id=channel_id,
                direction=direction,
                endpoint=endpoint,
            )
            session.status = SessionStatus.RINGING
            self._index_channel(session_id, channel_id)
            self._ensure_bridge(session)
            if session.bridge and channel_id:
                self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            self._maybe_mark_answered(session, session.operator_leg, channel_state)
            if hasattr(self.scenario_handler, "on_operator_channel_created"):
                self.scenario_handler.on_operator_channel_created(session)
            logger.info(
                "Operator channel %s joined session %s", channel_id, session.session_id
            )
        else:
            # Inbound calls are keyed by their channel id for the session id.
            session_id = channel_id
            session = Session(session_id=session_id)
            session.inbound_leg = CallLeg(
                channel_id=channel_id,
                direction=direction,
                endpoint=channel.get("caller", {}).get("number", "unknown"),
            )
            session.status = SessionStatus.RINGING
            with self.lock:
                self.sessions[session_id] = session
            self._index_channel(session_id, channel_id)
            self._ensure_bridge(session)
            if session.bridge and channel_id:
                self.ari_client.add_channel_to_bridge(session.bridge.bridge_id, channel_id)
            self._maybe_mark_answered(session, session.inbound_leg, channel_state)
            self.scenario_handler.on_inbound_channel_created(session)
            logger.info("Inbound channel %s created session %s", channel_id, session_id)

    def _handle_channel_state_change(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        channel_state = channel.get("state")
        session = self._get_session_by_channel(channel_id)
        if not session:
            logger.debug("Channel state change for unknown channel %s", channel_id)
            return

        leg = self._find_leg(session, channel_id)
        if leg is None:
            logger.debug("No leg mapped for channel %s", channel_id)
            return

        if channel_state == "Up":
            leg.state = LegState.ANSWERED
            session.status = SessionStatus.ACTIVE
            self.scenario_handler.on_call_answered(session, leg)
        elif channel_state == "Ringing":
            leg.state = LegState.RINGING
        elif channel_state in {"Busy", "Failed"}:
            leg.state = LegState.FAILED
            session.status = SessionStatus.FAILED
            if leg.direction == LegDirection.OPERATOR:
                session.result = session.result or "failed:operator_failed"
            self.scenario_handler.on_call_failed(session, reason=channel_state)

    def _handle_hangup(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = self._get_session_by_channel(channel_id)
        if not session:
            return
        leg = self._find_leg(session, channel_id)
        if leg:
            leg.state = LegState.HUNGUP
        session.status = SessionStatus.COMPLETED
        self.scenario_handler.on_call_hangup(session)

    def _handle_channel_destroyed(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = self._get_session_by_channel(channel_id)
        if not session:
            return
        leg = self._find_leg(session, channel_id)
        if leg:
            leg.state = LegState.HUNGUP
        session.status = SessionStatus.COMPLETED
        self._cleanup_session(session)
        self.scenario_handler.on_call_finished(session)

    def _handle_playback_finished(self, event: dict) -> None:
        playback = event.get("playback", {})
        playback_id = playback.get("id")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = self._get_session_by_channel(channel_id) if channel_id else None
        if not session and playback_id:
            session = self._get_session_by_playback(playback_id)
        if not session:
            return
        self.scenario_handler.on_playback_finished(session, playback_id)

    def _handle_playback_started(self, event: dict) -> None:
        # No-op but avoids noisy "Unhandled event" logs.
        playback = event.get("playback", {})
        playback_id = playback.get("id")
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        if playback_id and channel_id:
            with self.lock:
                if playback_id not in self.playback_to_session:
                    session_id = self.channel_to_session.get(channel_id)
                    if session_id:
                        self.playback_to_session[playback_id] = session_id

    def _handle_stasis_end(self, event: dict) -> None:
        channel = event.get("channel", {})
        channel_id = channel.get("id")
        session = self._get_session_by_channel(channel_id)
        if session:
            self._cleanup_session(session)

    def _cleanup_session(self, session: Session) -> None:
        # Proactively hang up any remaining legs before cleaning.
        for leg in (session.inbound_leg, session.outbound_leg, session.operator_leg):
            if leg and leg.channel_id and leg.state not in {LegState.HUNGUP, LegState.FAILED}:
                try:
                    self.ari_client.hangup_channel(leg.channel_id)
                except Exception as exc:
                    logger.debug(
                        "Failed to hang up channel %s during cleanup: %s",
                        leg.channel_id,
                        exc,
                    )
        with self.lock:
            for channel_id in list(self.channel_to_session.keys()):
                if self.channel_to_session[channel_id] == session.session_id:
                    del self.channel_to_session[channel_id]
            self.sessions.pop(session.session_id, None)
        if session.bridge:
            try:
                self.ari_client.delete_bridge(session.bridge.bridge_id)
            except Exception as exc:
                logger.warning(
                    "Failed to delete bridge %s for session %s: %s",
                    session.bridge.bridge_id,
                    session.session_id,
                    exc,
                )
        logger.info("Cleaned session %s", session.session_id)

    def active_sessions_count(self) -> int:
        with self.lock:
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

    def _maybe_mark_answered(self, session: Session, leg: CallLeg, channel_state: Optional[str]) -> None:
        if channel_state == "Up":
            leg.state = LegState.ANSWERED
            session.status = SessionStatus.ACTIVE
            self.scenario_handler.on_call_answered(session, leg)

    def register_playback(self, session_id: str, playback_id: str) -> None:
        with self.lock:
            self.playback_to_session[playback_id] = session_id

    def _get_session_by_playback(self, playback_id: str) -> Optional[Session]:
        with self.lock:
            session_id = self.playback_to_session.get(playback_id)
        if session_id:
            return self.get_session(session_id)
        return None
