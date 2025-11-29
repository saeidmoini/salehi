from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Set


class LegDirection(str, Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    OPERATOR = "operator"


class LegState(str, Enum):
    CREATED = "created"
    RINGING = "ringing"
    ANSWERED = "answered"
    HUNGUP = "hungup"
    FAILED = "failed"


class SessionStatus(str, Enum):
    INITIATING = "initiating"
    RINGING = "ringing"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class CallLeg:
    channel_id: str
    direction: LegDirection
    endpoint: str
    state: LegState = LegState.CREATED
    variables: Dict[str, str] = field(default_factory=dict)


@dataclass
class BridgeInfo:
    bridge_id: str
    bridge_type: str = "mixing"
    channels: List[str] = field(default_factory=list)


@dataclass
class Session:
    session_id: str
    bridge: Optional[BridgeInfo] = None
    inbound_leg: Optional[CallLeg] = None
    outbound_leg: Optional[CallLeg] = None
    operator_leg: Optional[CallLeg] = None
    status: SessionStatus = SessionStatus.INITIATING
    metadata: Dict[str, str] = field(default_factory=dict)
    playbacks: Dict[str, str] = field(default_factory=dict)
    responses: List[Dict[str, str]] = field(default_factory=list)
    result: Optional[str] = None
    processed_recordings: Set[str] = field(default_factory=set)

    def add_channel(self, channel_id: str) -> None:
        if not self.bridge:
            return
        if channel_id not in self.bridge.channels:
            self.bridge.channels.append(channel_id)
