"""Tests for inbound-direct call handling.

Inbound calls skip the marketing scenario and are directly connected
to an available agent's mobile phone. All inbound results are reported
as "disconnected" to the panel.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from sessions.session import (
    BridgeInfo,
    CallLeg,
    LegDirection,
    LegState,
    Session,
    SessionStatus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_settings(scenario_name: str = "salehi", transfer_to_operator: bool = False):
    """Build a minimal Settings-like object for MarketingScenario."""
    settings = MagicMock()
    settings.scenario.name = scenario_name
    settings.scenario.transfer_to_operator = transfer_to_operator
    settings.scenario.audio_src_dir = f"assets/audio/{scenario_name}/src"
    settings.operator.endpoint = ""
    settings.operator.extension = "200"
    settings.operator.trunk = "TO-CUCM-Gaptel"
    settings.operator.caller_id = "2000"
    settings.operator.timeout = 30
    settings.operator.mobile_numbers = ["09121111111", "09122222222"]
    settings.operator.use_panel_agents = False
    settings.dialer.outbound_trunk = "TO-CUCM-Gaptel"
    return settings


def _make_session(session_id: str = "inbound-ch-001") -> Session:
    """Create a Session with an inbound leg (mimicking session_manager behaviour)."""
    session = Session(session_id=session_id)
    session.inbound_leg = CallLeg(
        channel_id=session_id,
        direction=LegDirection.INBOUND,
        endpoint="09369000001",
        state=LegState.ANSWERED,
    )
    session.bridge = BridgeInfo(bridge_id="bridge-001")
    session.metadata["caller_number"] = "09369000001"
    session.metadata["contact_number"] = "09369000001"
    return session


def _build_scenario(settings=None, agent_mobiles=None):
    """Instantiate MarketingScenario with mocked dependencies."""
    from logic.marketing_outreach import MarketingScenario

    if settings is None:
        settings = _make_settings()
    ari = AsyncMock()
    ari.play_on_channel = AsyncMock(return_value={"id": "playback-1"})
    ari.originate_call = AsyncMock()
    ari.hangup_channel = AsyncMock()
    ari.stop_playback = AsyncMock()
    llm = AsyncMock()
    stt = AsyncMock()
    sm = AsyncMock()
    sm.register_playback = AsyncMock()
    panel = AsyncMock()

    scenario = MarketingScenario(settings, ari, llm, stt, sm, panel)
    # Attach a mock dialer with the helpers the scenario expects.
    dialer = MagicMock()
    dialer.operator_priority_requests = 0
    dialer.lock = asyncio.Lock()
    dialer.line_stats = {
        "02191302954": {"active": 0, "inbound_active": 0, "max_concurrent_calls": 5,
                        "attempts": [], "daily": 0, "last_originated_ts": 0},
    }
    dialer._available_line = MagicMock(return_value="02191302954")
    dialer._caller_id_for_line = MagicMock(return_value="2000")
    dialer._record_attempt = MagicMock()
    dialer.on_result = AsyncMock()
    dialer.on_session_completed = AsyncMock()
    scenario.attach_dialer(dialer)

    if agent_mobiles is not None:
        scenario.agent_mobiles = agent_mobiles
        scenario.agent_ids = {m: idx for idx, m in enumerate(agent_mobiles)}

    return scenario


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestInboundDirectCreation:
    """on_inbound_channel_created should mark the session and connect to operator."""

    @pytest.mark.asyncio
    async def test_marks_inbound_direct(self):
        scenario = _build_scenario()
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        assert session.metadata["inbound_direct"] == "1"

    @pytest.mark.asyncio
    async def test_plays_onhold_music(self):
        scenario = _build_scenario()
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        # Should have played onhold on the customer channel.
        scenario.ari_client.play_on_channel.assert_called()
        call_args_list = scenario.ari_client.play_on_channel.call_args_list
        media_args = [c[0][1] for c in call_args_list]
        assert "sound:custom/onhold" in media_args

    @pytest.mark.asyncio
    async def test_originates_operator_call(self):
        scenario = _build_scenario()
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        scenario.ari_client.originate_call.assert_called_once()
        call_kwargs = scenario.ari_client.originate_call.call_args
        endpoint = call_kwargs.kwargs.get("endpoint") or call_kwargs[1].get("endpoint")
        assert "09121111111" in endpoint or "09122222222" in endpoint

    @pytest.mark.asyncio
    async def test_operator_call_started_metadata(self):
        scenario = _build_scenario()
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        assert session.metadata.get("operator_call_started") == "1"


class TestInboundDirectSkipsMarketing:
    """on_call_answered should NOT play 'hello' for inbound-direct sessions."""

    @pytest.mark.asyncio
    async def test_skips_hello_prompt(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"

        inbound_leg = session.inbound_leg
        await scenario.on_call_answered(session, inbound_leg)

        # Should NOT have played "hello".
        for call in scenario.ari_client.play_on_channel.call_args_list:
            media = call[0][1] if len(call[0]) > 1 else call.kwargs.get("media", "")
            assert "hello" not in media

    @pytest.mark.asyncio
    async def test_sets_answered_at(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"

        await scenario.on_call_answered(session, session.inbound_leg)

        assert "answered_at" in session.metadata


class TestInboundDirectOperatorAnswer:
    """When the operator answers an inbound-direct call, result should be 'disconnected'."""

    @pytest.mark.asyncio
    async def test_result_is_disconnected(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.metadata["operator_connected"] = "0"

        operator_leg = CallLeg(
            channel_id="op-ch-001",
            direction=LegDirection.OPERATOR,
            endpoint="09121111111",
            state=LegState.ANSWERED,
        )
        session.operator_leg = operator_leg

        await scenario.on_call_answered(session, operator_leg)

        assert session.result == "disconnected"
        assert session.metadata["operator_connected"] == "1"

    @pytest.mark.asyncio
    async def test_outbound_operator_answer_is_connected(self):
        """For regular outbound calls, operator answer should be 'connected_to_operator'."""
        scenario = _build_scenario()
        session = Session(session_id="outbound-001")
        session.outbound_leg = CallLeg(
            channel_id="out-ch-001",
            direction=LegDirection.OUTBOUND,
            endpoint="09369000001",
        )
        session.bridge = BridgeInfo(bridge_id="bridge-002")

        operator_leg = CallLeg(
            channel_id="op-ch-002",
            direction=LegDirection.OPERATOR,
            endpoint="09121111111",
            state=LegState.ANSWERED,
        )
        session.operator_leg = operator_leg

        await scenario.on_call_answered(session, operator_leg)

        assert session.result == "connected_to_operator"


class TestInboundDirectCallFinished:
    """on_call_finished should force result to 'disconnected' for inbound-direct."""

    @pytest.mark.asyncio
    async def test_default_result_is_disconnected(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.result = None

        await scenario.on_call_finished(session)

        assert session.result == "disconnected"

    @pytest.mark.asyncio
    async def test_overrides_other_results(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.result = "connected_to_operator"

        await scenario.on_call_finished(session)

        assert session.result == "disconnected"


class TestInboundDirectCallHangup:
    """Hangup during inbound-direct should always report disconnected."""

    @pytest.mark.asyncio
    async def test_hangup_during_operator_ring(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.metadata["operator_call_started"] = "1"
        session.operator_leg = CallLeg(
            channel_id="op-ch-003",
            direction=LegDirection.OPERATOR,
            endpoint="09121111111",
            state=LegState.RINGING,
        )

        await scenario.on_call_hangup(session)

        assert session.result == "disconnected"

    @pytest.mark.asyncio
    async def test_hangup_after_operator_connected(self):
        scenario = _build_scenario()
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.metadata["operator_connected"] = "1"

        await scenario.on_call_hangup(session)

        assert session.result == "disconnected"


class TestInboundDirectOperatorFailed:
    """When all operator retries fail for inbound-direct, result is disconnected."""

    @pytest.mark.asyncio
    async def test_no_retry_agents_available(self):
        """When operator leg fails and no agents are configured, result should be disconnected."""
        scenario = _build_scenario(agent_mobiles=[])
        session = _make_session()
        session.metadata["inbound_direct"] = "1"
        session.metadata["operator_call_started"] = "1"
        session.metadata["operator_mobile"] = "09121111111"
        session.metadata["operator_outbound_line"] = "02191302954"
        session.operator_leg = CallLeg(
            channel_id="op-ch-004",
            direction=LegDirection.OPERATOR,
            endpoint="09121111111",
            state=LegState.FAILED,
        )

        await scenario.on_call_failed(session, "Failed")

        assert session.result == "disconnected"


class TestInboundDirectNoAgents:
    """When no agents are available, inbound-direct should hangup with disconnected."""

    @pytest.mark.asyncio
    async def test_no_agents_available(self):
        scenario = _build_scenario(agent_mobiles=["09121111111"])
        # Mark the only agent as busy.
        scenario.agent_busy.add("09121111111")
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        assert session.result == "disconnected"
        scenario.ari_client.hangup_channel.assert_called()


class TestInboundDirectBothScenarios:
    """Inbound-direct should work the same for both salehi and agrad scenarios."""

    @pytest.mark.asyncio
    async def test_salehi_scenario(self):
        settings = _make_settings(scenario_name="salehi", transfer_to_operator=False)
        scenario = _build_scenario(settings=settings)
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        assert session.metadata["inbound_direct"] == "1"
        scenario.ari_client.originate_call.assert_called_once()

    @pytest.mark.asyncio
    async def test_agrad_scenario(self):
        settings = _make_settings(scenario_name="agrad", transfer_to_operator=True)
        scenario = _build_scenario(settings=settings)
        session = _make_session()

        await scenario.on_inbound_channel_created(session)

        assert session.metadata["inbound_direct"] == "1"
        scenario.ari_client.originate_call.assert_called_once()
