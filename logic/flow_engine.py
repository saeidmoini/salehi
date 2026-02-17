"""
Flow execution engine: interprets YAML-defined call flows.

Replaces MarketingScenario with a generic step-based engine that can run
any scenario defined in config/scenarios/*.yaml.
"""
import asyncio
import io
import logging
import time
import audioop
import wave
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

import httpx

from config.flow_definition import ScenarioConfig
from config.settings import Settings
from core.ari_client import AriClient
from integrations.panel.client import PanelClient
from llm.client import GapGPTClient
from logic.base import BaseScenario
from logic.scenario_registry import ScenarioRegistry
from sessions.session import CallLeg, LegDirection, LegState, Session
from stt_tts.vira_stt import STTResult, ViraSTTClient


logger = logging.getLogger(__name__)


class FlowEngine(BaseScenario):
    """
    Generic flow execution engine driven by YAML scenario definitions.

    Maintains per-scenario agent rosters and delegates to the appropriate
    scenario config based on session.metadata["scenario_name"].
    """

    def __init__(
        self,
        settings: Settings,
        ari_client: AriClient,
        llm_client: GapGPTClient,
        stt_client: ViraSTTClient,
        session_manager,  # forward ref to avoid circular import
        registry: ScenarioRegistry,
        panel_client: Optional[PanelClient] = None,
    ):
        self.settings = settings
        self.ari_client = ari_client
        self.llm_client = llm_client
        self.stt_client = stt_client
        self.session_manager = session_manager
        self.registry = registry
        self.panel_client = panel_client
        self.dialer = None

        # Per-scenario agent rosters
        # inbound_agents: list of {phone_number, id} for inbound calls
        # outbound_agents: list of {phone_number, id} for outbound operator transfer
        self.inbound_agents: list[dict] = []
        self.outbound_agents: list[dict] = []
        self.agent_busy: set[str] = set()
        self.inbound_agent_cursor: int = 0
        self.outbound_agent_cursor: int = 0

        # Fall back to static operator mobiles from settings
        for m in settings.operator.mobile_numbers:
            if m:
                self.outbound_agents.append({"phone_number": m, "id": None})

        # Transcript loggers
        self.positive_logger = self._build_log("logic.positives", "positive_stt.log")
        self.negative_logger = self._build_log("logic.negatives", "negative_stt.log")
        self.unknown_logger = self._build_log("logic.unknowns", "unknown_stt.log")

        logger.info("FlowEngine initialized with %d scenarios", len(registry.get_names()))

    def attach_dialer(self, dialer) -> None:
        self.dialer = dialer

    # -- Agent management --------------------------------------------------

    async def set_inbound_agents(self, agents: list) -> None:
        """Replace inbound agent roster from panel."""
        parsed = self._parse_agents(agents)
        if parsed:
            self.inbound_agents = parsed
            self.inbound_agent_cursor = 0
            logger.info("Updated inbound agents: %d", len(parsed))

    async def set_outbound_agents(self, agents: list) -> None:
        """Replace outbound agent roster from panel."""
        parsed = self._parse_agents(agents)
        if parsed:
            self.outbound_agents = parsed
            self.outbound_agent_cursor = 0
            logger.info("Updated outbound agents: %d", len(parsed))

    # Backward compat: set_panel_agents sets outbound agents (legacy behavior)
    async def set_panel_agents(self, agents: list) -> None:
        await self.set_outbound_agents(agents)

    def _parse_agents(self, agents: list) -> list[dict]:
        result = []
        for agent in agents:
            if isinstance(agent, dict):
                phone = agent.get("phone_number")
                agent_id = agent.get("id")
            else:
                phone = getattr(agent, "phone_number", None)
                agent_id = getattr(agent, "id", None)
            if phone:
                result.append({"phone_number": phone, "id": agent_id})
        return result

    def _next_available_agent(self, agent_type: str = "outbound") -> Optional[dict]:
        """Round-robin pick from the appropriate agent list, skipping busy."""
        agents = self.inbound_agents if agent_type == "inbound" else self.outbound_agents
        if not agents:
            return None
        n = len(agents)
        cursor_attr = "inbound_agent_cursor" if agent_type == "inbound" else "outbound_agent_cursor"
        cursor = getattr(self, cursor_attr)
        for i in range(n):
            idx = (cursor + i) % n
            agent = agents[idx]
            if agent["phone_number"] not in self.agent_busy:
                setattr(self, cursor_attr, (idx + 1) % n)
                return agent
        return None

    # -- Scenario helpers --------------------------------------------------

    def _get_scenario(self, session: Session) -> Optional[ScenarioConfig]:
        name = session.metadata.get("scenario_name")
        if not name:
            return None
        return self.registry.get(name)

    def _is_inbound(self, session: Session) -> bool:
        return session.inbound_leg is not None and session.outbound_leg is None

    def _customer_channel_id(self, session: Session) -> Optional[str]:
        if session.outbound_leg:
            return session.outbound_leg.channel_id
        if session.inbound_leg:
            return session.inbound_leg.channel_id
        return None

    # -- BaseScenario hooks ------------------------------------------------

    async def on_outbound_channel_created(self, session: Session) -> None:
        logger.debug("Outbound channel ready for session %s", session.session_id)

    async def on_inbound_channel_created(self, session: Session) -> None:
        """
        Handle inbound calls:
        - If a scenario with inbound_flow is assigned, run that flow.
        - Otherwise, direct-to-agent (default behavior).
        """
        scenario = self._get_scenario(session)
        if scenario and scenario.inbound_flow:
            # Run scenario's inbound flow
            logger.info("Running inbound flow '%s' for session %s", scenario.name, session.session_id)
            entry = scenario.get_entry_step(inbound=True)
            if entry:
                await self._execute_step(session, entry, inbound=True)
        else:
            # Default: direct-to-agent
            async with session.lock:
                session.metadata["inbound_direct"] = "1"
            logger.info("Inbound call session %s – connecting directly to agent", session.session_id)
            await self._play_onhold(session)
            await self._connect_to_operator(session, agent_type="inbound")

    async def on_operator_channel_created(self, session: Session) -> None:
        logger.debug("Operator leg created for session %s", session.session_id)

    async def on_call_answered(self, session: Session, leg: CallLeg) -> None:
        if leg.direction == LegDirection.OPERATOR:
            async with session.lock:
                is_inbound_direct = session.metadata.get("inbound_direct") == "1"
                session.result = session.result or (
                    "inbound_call" if is_inbound_direct else "connected_to_operator"
                )
                session.metadata["operator_connected"] = "1"
            await self._stop_onhold_playbacks(session)
            logger.info("Operator leg answered for session %s (inbound_direct=%s)", session.session_id, is_inbound_direct)
            return

        # Inbound-direct sessions skip marketing flow
        async with session.lock:
            if session.metadata.get("inbound_direct") == "1":
                session.metadata["answered_at"] = str(time.time())
                logger.info("Inbound-direct call answered for session %s – waiting for operator", session.session_id)
                return

        async with session.lock:
            session.metadata["answered_at"] = str(time.time())
        logger.info("Call answered for session %s (customer)", session.session_id)

        # Start the outbound flow
        scenario = self._get_scenario(session)
        if not scenario:
            logger.warning("No scenario for session %s; hanging up", session.session_id)
            await self._hangup(session)
            return
        entry = scenario.get_entry_step(inbound=False)
        if entry:
            await self._execute_step(session, entry, inbound=False)

    async def on_playback_finished(self, session: Session, playback_id: str) -> None:
        async with session.lock:
            prompt_key = session.playbacks.pop(playback_id, None)
        if not prompt_key:
            return
        logger.debug("Playback %s finished for session %s (%s)", playback_id, session.session_id, prompt_key)

        # onhold loops
        if prompt_key == "onhold":
            operator_connected = False
            async with session.lock:
                operator_connected = session.metadata.get("operator_connected") == "1"
            if not operator_connected:
                await self._play_onhold(session)
            return

        # Resume flow from the step that played this prompt
        async with session.lock:
            pending_step = session.metadata.pop("pending_playback_step", None)
            pending_next = session.metadata.pop("pending_playback_next", None)
            is_inbound = session.metadata.get("flow_inbound") == "1"
        if pending_next:
            scenario = self._get_scenario(session)
            if scenario:
                next_step = scenario.get_step(pending_next, inbound=is_inbound)
                if next_step:
                    await self._execute_step(session, next_step, inbound=is_inbound)

    async def on_recording_finished(self, session: Session, recording_name: str) -> None:
        async with session.lock:
            phase = session.metadata.get("recording_phase")
            if not phase or session.metadata.get("recording_name") != recording_name:
                return
            if recording_name in session.processed_recordings:
                return
            session.processed_recordings.add(recording_name)
            is_inbound = session.metadata.get("flow_inbound") == "1"
            next_step_id = session.metadata.get("pending_record_next")
            on_empty_id = session.metadata.get("pending_record_on_empty")
            on_failure_id = session.metadata.get("pending_record_on_failure")

        # Process in background
        asyncio.create_task(
            self._process_recording(session, recording_name, phase, is_inbound,
                                     next_step_id, on_empty_id, on_failure_id)
        )

    async def on_recording_failed(self, session: Session, recording_name: str, cause: str) -> None:
        async with session.lock:
            phase = session.metadata.get("recording_phase")
            if not phase or session.metadata.get("recording_name") != recording_name:
                return
            if recording_name in session.processed_recordings:
                return
            session.processed_recordings.add(recording_name)
            if session.metadata.get("hungup") == "1":
                return
            is_inbound = session.metadata.get("flow_inbound") == "1"
            on_failure_id = session.metadata.get("pending_record_on_failure")

        logger.warning("Recording failed (phase=%s) for session %s cause=%s", phase, session.session_id, cause)
        scenario = self._get_scenario(session)
        if scenario and on_failure_id:
            step = scenario.get_step(on_failure_id, inbound=is_inbound)
            if step:
                await self._execute_step(session, step, inbound=is_inbound)

    async def on_call_failed(self, session: Session, reason: str) -> None:
        if session.result:
            logger.debug("Skipping duplicate on_call_failed for session=%s (already has result=%s)",
                        session.session_id, session.result)
            return

        operator_failed = session.operator_leg and session.operator_leg.state == LegState.FAILED
        if operator_failed:
            retried = await self._retry_operator_mobile(session, reason)
            if retried:
                return
            await self._stop_onhold_playbacks(session)
            async with session.lock:
                yes_intent = session.metadata.get("intent_yes") == "1"
                is_inbound_direct = session.metadata.get("inbound_direct") == "1"
            current_result = session.result
            if is_inbound_direct:
                result_value = "disconnected"
            elif current_result and current_result.startswith("failed:operator"):
                result_value = current_result
            else:
                result_value = "disconnected" if yes_intent else "hangup"
            await self._set_result(session, result_value, force=True, report=True)
            await self._hangup(session)
            return

        # Customer leg failed - classify based on cause codes
        reason_l = reason.lower() if reason else ""
        hangup_cause = session.metadata.get("hangup_cause")
        dialstatus = session.metadata.get("dialstatus", "")

        if hangup_cause in {"16", "31", "32"}:
            result_value = "hangup"
        elif hangup_cause == "17":
            result_value = "busy"
        elif hangup_cause in {"18", "19", "20"}:
            result_value = "power_off"
        elif hangup_cause in {"0", "1", "3", "22", "27"}:
            result_value = "power_off"
        elif hangup_cause == "38":
            result_value = "power_off"
            logger.info("Iran telecom cause=38 -> power_off: session=%s", session.session_id)
        elif hangup_cause in {"21", "34", "41", "42"}:
            result_value = "banned"
        elif "busy" in reason_l:
            result_value = "busy"
        elif "congest" in reason_l or "failed" in reason_l:
            result_value = "banned"
        else:
            result_value = "missed"

        await self._set_result(session, result_value, force=True, report=True)
        logger.warning("Call failed session=%s reason=%s cause=%s result=%s",
                       session.session_id, reason, hangup_cause, result_value)
        await self._hangup(session)

    async def on_call_hangup(self, session: Session) -> None:
        operator_connected = False
        yes_intent = False
        no_intent = False
        app_hangup = False
        operator_call_started = False
        is_inbound_direct = False
        cause = None
        async with session.lock:
            session.metadata["hungup"] = "1"
            operator_connected = session.metadata.get("operator_connected") == "1"
            yes_intent = session.metadata.get("intent_yes") == "1"
            no_intent = session.metadata.get("intent_no") == "1"
            app_hangup = session.metadata.get("app_hangup") == "1"
            operator_call_started = session.metadata.get("operator_call_started") == "1"
            is_inbound_direct = session.metadata.get("inbound_direct") == "1"
            cause = session.metadata.get("hangup_cause")

        if operator_connected:
            if is_inbound_direct:
                await self._set_result(session, "inbound_call", force=True, report=True)
            return

        if operator_call_started and session.operator_leg and session.operator_leg.channel_id:
            try:
                await self.ari_client.hangup_channel(session.operator_leg.channel_id)
            except Exception as exc:
                logger.debug("Failed to hangup pending operator leg for session %s: %s", session.session_id, exc)
            async with session.lock:
                session.metadata.pop("operator_mobile", None)
                session.metadata.pop("operator_outbound_line", None)
            await self._set_result(session, "disconnected", force=True, report=True)
            await self._stop_onhold_playbacks(session)
            return

        # Map cause codes
        cause_result = None
        if cause:
            cause_map = {"17": "busy", "21": "banned", "18": "power_off", "19": "power_off",
                        "20": "power_off", "34": "banned", "41": "banned", "42": "banned"}
            cause_result = cause_map.get(cause)
        else:
            cause_txt = session.metadata.get("hangup_cause_txt", "")
            if "Request Terminated" in cause_txt:
                cause_result = "missed"
            elif "Busy" in cause_txt:
                cause_result = "busy"
            elif "Congested" in cause_txt:
                cause_result = "banned"

        if cause_result and (session.result is None or session.result in {"user_didnt_answer", "missed", "hangup", "disconnected"}):
            await self._set_result(session, cause_result, force=True, report=True)
            await self._stop_onhold_playbacks(session)
            await self._hangup(session)
            return

        if session.result is None or session.result in {"user_didnt_answer", "missed"}:
            if yes_intent:
                await self._set_result(session, "disconnected", force=True, report=True)
            elif no_intent:
                await self._set_result(session, "not_interested", force=True, report=True)
            elif not app_hangup:
                await self._set_result(session, "hangup", force=True, report=True)
            else:
                await self._set_result(session, session.result or "failed:hangup", force=True, report=True)
        await self._stop_onhold_playbacks(session)

    async def on_call_finished(self, session: Session) -> None:
        async with session.lock:
            is_inbound_direct = session.metadata.get("inbound_direct") == "1"
            if session.result is None:
                session.result = "inbound_call" if is_inbound_direct else "user_didnt_answer"
            elif is_inbound_direct and session.result not in ("inbound_call", "disconnected"):
                session.result = "inbound_call"
            result = session.result
            operator_mobile = session.metadata.get("operator_mobile")
        if operator_mobile:
            self.agent_busy.discard(operator_mobile)
        line_used = session.metadata.get("operator_outbound_line")
        if line_used:
            await self._release_outbound_line(line_used)
        logger.info("Call finished session=%s result=%s", session.session_id, result)
        await self._report_result(session)
        if self.dialer:
            await self.dialer.on_session_completed(session.session_id)

    # -- Flow execution engine ---------------------------------------------

    async def _execute_step(self, session: Session, step, inbound: bool = False) -> None:
        """Execute a flow step and chain to the next one."""
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
            session.metadata["flow_inbound"] = "1" if inbound else "0"
            session.metadata["current_step"] = step.step

        scenario = self._get_scenario(session)
        if not scenario:
            return

        logger.debug("Executing step '%s' (type=%s) for session %s", step.step, step.type, session.session_id)

        if step.type == "entry":
            if step.next:
                next_step = scenario.get_step(step.next, inbound=inbound)
                if next_step:
                    await self._execute_step(session, next_step, inbound=inbound)

        elif step.type == "play_prompt":
            # Play prompt, then pause — resumed by on_playback_finished
            async with session.lock:
                session.metadata["pending_playback_step"] = step.step
                session.metadata["pending_playback_next"] = step.next or ""
            prompt_key = step.prompt
            if prompt_key:
                await self._play_prompt(session, prompt_key, scenario)

        elif step.type == "record":
            await self._start_recording(session, step, scenario, inbound)

        elif step.type == "classify_intent":
            await self._classify_intent_step(session, step, scenario, inbound)

        elif step.type == "route_by_intent":
            await self._route_by_intent_step(session, step, scenario, inbound)

        elif step.type == "check_retry_limit":
            await self._check_retry_limit_step(session, step, scenario, inbound)

        elif step.type == "set_result":
            if step.result:
                await self._set_result(session, step.result, force=True, report=True)
            if step.next:
                next_step = scenario.get_step(step.next, inbound=inbound)
                if next_step:
                    await self._execute_step(session, next_step, inbound=inbound)

        elif step.type == "transfer_to_operator":
            agent_type = step.agent_type or "outbound"
            async with session.lock:
                session.metadata["transfer_on_success"] = step.on_success or ""
                session.metadata["transfer_on_failure"] = step.on_failure or ""
            await self._play_onhold(session)
            await self._connect_to_operator(session, agent_type=agent_type)

        elif step.type == "disconnect":
            await self._hangup(session)

        elif step.type == "hangup":
            await self._hangup(session)

        elif step.type == "wait":
            # Just pause — call stays bridged until someone hangs up
            pass

        else:
            logger.warning("Unknown step type '%s' in step '%s'", step.type, step.step)

    # -- Step implementations ----------------------------------------------

    async def _play_prompt(self, session: Session, prompt_key: str, scenario: Optional[ScenarioConfig] = None) -> None:
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
        if scenario is None:
            scenario = self._get_scenario(session)
        media = None
        if scenario and prompt_key in scenario.prompts:
            media = scenario.prompts[prompt_key]
        else:
            # Fallback to common prompts
            media = f"sound:custom/{prompt_key}"
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No customer channel to play %s for session %s", prompt_key, session.session_id)
            return
        try:
            playback = await self.ari_client.play_on_channel(channel_id, media)
        except Exception as exc:
            logger.warning("Failed to play %s on %s for session %s: %s", prompt_key, channel_id, session.session_id, exc)
            return
        playback_id = playback.get("id")
        if playback_id:
            async with session.lock:
                session.playbacks[playback_id] = prompt_key
            await self.session_manager.register_playback(session.session_id, playback_id)
        logger.info("Playing prompt %s on channel %s", prompt_key, channel_id)

    async def _play_onhold(self, session: Session) -> None:
        scenario = self._get_scenario(session)
        await self._play_prompt(session, "onhold", scenario)

    async def _stop_onhold_playbacks(self, session: Session) -> None:
        async with session.lock:
            hold_playbacks = [pb_id for pb_id, key in session.playbacks.items() if key == "onhold"]
        for pb_id in hold_playbacks:
            try:
                await self.ari_client.stop_playback(pb_id)
            except Exception as exc:
                logger.debug("Failed to stop onhold playback %s: %s", pb_id, exc)

    async def _start_recording(self, session: Session, step, scenario: ScenarioConfig, inbound: bool) -> None:
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            return
        phase = step.step
        recording_name = f"{phase}-{session.session_id}"
        async with session.lock:
            session.metadata["recording_phase"] = phase
            session.metadata["recording_name"] = recording_name
            session.metadata["pending_record_next"] = step.next or ""
            session.metadata["pending_record_on_empty"] = step.on_empty or ""
            session.metadata["pending_record_on_failure"] = step.on_failure or ""
            if session.metadata.get("hungup") == "1":
                return
        logger.info("Recording %s for session %s", phase, session.session_id)
        try:
            if session.bridge and session.bridge.bridge_id:
                await self.ari_client.record_bridge(
                    bridge_id=session.bridge.bridge_id,
                    name=recording_name,
                    max_duration=scenario.stt.max_duration,
                    max_silence=scenario.stt.max_silence,
                )
            else:
                await self.ari_client.record_channel(
                    channel_id=channel_id,
                    name=recording_name,
                    max_duration=scenario.stt.max_duration,
                    max_silence=scenario.stt.max_silence,
                )
            await self.session_manager.register_recording(session.session_id, recording_name)
        except Exception as exc:
            logger.exception("Failed to start recording for session %s: %s", session.session_id, exc)
            if step.on_failure:
                fail_step = scenario.get_step(step.on_failure, inbound=inbound)
                if fail_step:
                    await self._execute_step(session, fail_step, inbound=inbound)

    async def _process_recording(
        self, session: Session, recording_name: str, phase: str, inbound: bool,
        next_step_id: Optional[str], on_empty_id: Optional[str], on_failure_id: Optional[str],
    ) -> None:
        """Fetch recording, transcribe, store transcript in session."""
        scenario = self._get_scenario(session)
        if not scenario:
            return
        try:
            audio_bytes = await self.ari_client.fetch_stored_recording(recording_name)
            if self._is_empty_audio(audio_bytes):
                logger.info("Recording empty for session %s", session.session_id)
                if on_empty_id:
                    step = scenario.get_step(on_empty_id, inbound=inbound)
                    if step:
                        await self._execute_step(session, step, inbound=inbound)
                return

            stt_result: STTResult = await self.stt_client.transcribe_audio(
                audio_bytes, hotwords=scenario.stt.hotwords,
            )
            async with session.lock:
                if session.metadata.get("hungup") == "1":
                    return
            transcript = stt_result.text.strip()
            logger.info("STT (%s) session %s: %s", phase, session.session_id, transcript)

            if not transcript:
                if on_empty_id:
                    step = scenario.get_step(on_empty_id, inbound=inbound)
                    if step:
                        await self._execute_step(session, step, inbound=inbound)
                return

            # Store transcript for later classification
            async with session.lock:
                session.metadata["last_transcript"] = transcript
                session.responses.append({"phase": phase, "text": transcript})

            # Continue to next step (usually classify_intent)
            if next_step_id:
                step = scenario.get_step(next_step_id, inbound=inbound)
                if step:
                    await self._execute_step(session, step, inbound=inbound)

        except Exception as exc:
            logger.exception("Transcription failed for session %s: %s", session.session_id, exc)
            msg = str(exc)
            # Vira balance error
            if "403" in msg or "balanceError" in msg or "credit is below the set threshold" in msg:
                await self._handle_quota_error(session, "failed:vira_quota")
                return
            # Empty audio from Vira
            if "Empty Audio file" in msg or "Input file content is unexpected" in msg:
                await self._set_result(session, "hangup", force=True, report=True)
                return
            if on_failure_id:
                step = scenario.get_step(on_failure_id, inbound=inbound)
                if step:
                    await self._execute_step(session, step, inbound=inbound)

    async def _classify_intent_step(self, session: Session, step, scenario: ScenarioConfig, inbound: bool) -> None:
        """Classify the last transcript using LLM, store intent."""
        async with session.lock:
            transcript = session.metadata.get("last_transcript", "")
        if not transcript:
            if step.on_failure:
                fail_step = scenario.get_step(step.on_failure, inbound=inbound)
                if fail_step:
                    await self._execute_step(session, fail_step, inbound=inbound)
            return
        try:
            intent = await self._detect_intent(transcript, scenario)
        except Exception as exc:
            logger.warning("Intent classification failed for session %s: %s", session.session_id, exc)
            if self._is_llm_quota_error(exc):
                await self._handle_quota_error(session, "failed:llm_quota")
                return
            intent = "unknown"

        async with session.lock:
            session.metadata["last_intent"] = intent
            if session.responses:
                session.responses[-1]["intent"] = intent
            if intent == "yes":
                session.metadata["intent_yes"] = "1"
            elif intent == "no":
                session.metadata["intent_no"] = "1"

        # Log transcript by intent
        if intent == "yes":
            self.positive_logger.info("session=%s transcript=%s", session.session_id, transcript)
        elif intent == "no":
            self.negative_logger.info("session=%s transcript=%s", session.session_id, transcript)
        else:
            self.unknown_logger.info("session=%s intent=%s transcript=%s", session.session_id, intent, transcript)

        if step.next:
            next_step = scenario.get_step(step.next, inbound=inbound)
            if next_step:
                await self._execute_step(session, next_step, inbound=inbound)

    async def _route_by_intent_step(self, session: Session, step, scenario: ScenarioConfig, inbound: bool) -> None:
        """Branch by last_intent."""
        async with session.lock:
            intent = session.metadata.get("last_intent", "unknown")
        if step.routes and intent in step.routes:
            target_id = step.routes[intent]
        elif step.routes and "unknown" in step.routes:
            target_id = step.routes["unknown"]
        else:
            logger.warning("No route for intent '%s' in step '%s'", intent, step.step)
            return
        target_step = scenario.get_step(target_id, inbound=inbound)
        if target_step:
            await self._execute_step(session, target_step, inbound=inbound)

    async def _check_retry_limit_step(self, session: Session, step, scenario: ScenarioConfig, inbound: bool) -> None:
        """Check a counter and branch accordingly."""
        counter_key = step.counter or "retry_count"
        async with session.lock:
            count = int(session.metadata.get(counter_key, "0"))
            count += 1
            session.metadata[counter_key] = str(count)
        max_count = step.max_count or 1
        if count <= max_count and step.within_limit:
            target = scenario.get_step(step.within_limit, inbound=inbound)
            if target:
                await self._execute_step(session, target, inbound=inbound)
        elif step.exceeded:
            target = scenario.get_step(step.exceeded, inbound=inbound)
            if target:
                await self._execute_step(session, target, inbound=inbound)

    # -- Intent detection --------------------------------------------------

    async def _detect_intent(self, transcript: str, scenario: ScenarioConfig) -> str:
        """Detect intent using LLM with fallback to token matching."""
        text = transcript.lower()

        # Fast-path for clear yes tokens
        for token in ("بله", "آره"):
            if token in transcript:
                return "yes"

        if self.llm_client.api_key:
            llm_config = scenario.llm
            if llm_config.prompt_template:
                prompt = llm_config.prompt_template.format(
                    transcript=transcript,
                    intent_categories=", ".join(llm_config.intent_categories),
                )
            else:
                # Default prompt
                yes_examples = llm_config.fallback_tokens.get("yes", [])[:30]
                no_examples = llm_config.fallback_tokens.get("no", [])[:20]
                number_q_examples = llm_config.fallback_tokens.get("number_question", [
                    "شماره منو از کجا آوردی", "شماره منو از کجا آوردین",
                ])
                prompt = (
                    "Classify intent into one word: yes / no / number_question / unknown.\n"
                    "YES = interest or any question about price/place/time/links.\n"
                    f"Examples YES: {'; '.join(yes_examples)}.\n"
                    f"NO = reject/decline. Examples NO: {'; '.join(no_examples)}.\n"
                    f"NUMBER_QUESTION = asks where we got their number. Examples: {'; '.join(number_q_examples)}.\n"
                    f"User: {transcript}"
                )

            try:
                result = await self.llm_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model="gpt-4o-mini",
                    temperature=0,
                )
                normalized = result.strip().lower()
                intent = self._extract_intent_label(normalized)
                if intent:
                    return intent
            except Exception as exc:
                logger.warning("LLM intent fallback failed: %s", exc)
                if self._is_llm_quota_error(exc):
                    raise

        # Token-based fallback
        fallback = scenario.llm.fallback_tokens
        for token in fallback.get("yes", []):
            if token in text:
                return "yes"
        for token in fallback.get("no", []):
            if token in text:
                return "no"
        for token in fallback.get("number_question", []):
            if token in text:
                return "number_question"
        return "unknown"

    def _extract_intent_label(self, normalized: str) -> Optional[str]:
        tokens = [tok.strip(" ,.;!?") for tok in normalized.split() if tok.strip(" ,.;!?")]
        if tokens:
            first = tokens[0]
            if first in {"yes", "y", "yeah", "ok", "okay"}:
                return "yes"
            if first in {"no", "nah", "nope"}:
                return "no"
        if "number_question" in normalized or "number question" in normalized:
            return "number_question"
        return None

    def _is_llm_quota_error(self, exc: Exception) -> bool:
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
            if exc.response.status_code == 403:
                return True
            try:
                data = exc.response.json()
                err = data.get("error", {})
                code = err.get("code", "") or err.get("type", "")
                msg = (err.get("message") or "").lower()
                if "pre_consume_token_quota_failed" in code or "token quota is not enough" in msg:
                    return True
            except Exception:
                pass
        msg = str(exc).lower()
        return "pre_consume_token_quota_failed" in msg or "token quota is not enough" in msg or "403" in msg

    async def _handle_quota_error(self, session: Session, result: str) -> None:
        """Handle Vira/LLM quota errors: pause dialer, alert, hangup."""
        async with session.lock:
            session.metadata["panel_last_status"] = "FAILED"
        await self._set_result(session, result, force=True, report=True)
        if self.dialer:
            threshold = self.dialer.settings.sms.fail_alert_threshold
            self.dialer.failure_streak = max(self.dialer.failure_streak, threshold)
            await self.dialer.on_result(
                session.session_id, result,
                session.metadata.get("number_id"),
                session.metadata.get("contact_number"),
                session.metadata.get("batch_id"),
                session.metadata.get("attempted_at"),
            )
        await self._hangup(session)
        await self.session_manager._cleanup_session(session)

    # -- Operator bridge ---------------------------------------------------

    async def _reserve_outbound_line(self) -> Optional[str]:
        if not self.dialer:
            return None
        self.dialer.operator_priority_requests += 1
        try:
            deadline = time.monotonic() + max(self.settings.operator.timeout, 5)
            while time.monotonic() < deadline:
                line = self.dialer._available_line()
                if line:
                    async with self.dialer.lock:
                        stats = self.dialer.line_stats.get(line)
                        if stats is None:
                            return None
                        stats["active"] += 1
                        stats["attempts"].append(datetime.utcnow())
                        stats["daily"] += 1
                        stats["last_originated_ts"] = time.monotonic()
                    self.dialer._record_attempt()
                    return line
                await asyncio.sleep(0.05)
            return None
        finally:
            self.dialer.operator_priority_requests = max(0, self.dialer.operator_priority_requests - 1)

    async def _release_outbound_line(self, line: Optional[str]) -> None:
        if not line or not self.dialer:
            return
        async with self.dialer.lock:
            stats = self.dialer.line_stats.get(line)
            if stats:
                stats["active"] = max(stats.get("active", 0) - 1, 0)

    async def _connect_to_operator(self, session: Session, agent_type: str = "outbound") -> None:
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
            if session.metadata.get("operator_call_started") == "1":
                return
            session.metadata["operator_call_started"] = "1"
            session.metadata.pop("operator_tried", None)

        customer_channel = self._customer_channel_id(session)
        if not customer_channel:
            logger.warning("No customer channel for operator connect session %s", session.session_id)
            return

        endpoint = ""
        operator_mobile = None
        outbound_line = None
        is_inbound_direct = session.metadata.get("inbound_direct") == "1"

        agent = self._next_available_agent(agent_type)
        if agent:
            operator_mobile = agent["phone_number"]
            outbound_line = await self._reserve_outbound_line()
            if not outbound_line:
                logger.warning("No outbound line for operator session %s", session.session_id)
                if is_inbound_direct:
                    await self._set_result(session, "disconnected", force=True, report=True)
                    await self._hangup(session)
                return
            endpoint = f"PJSIP/{operator_mobile}@{self.settings.dialer.outbound_trunk}"
        elif not agent and (self.inbound_agents or self.outbound_agents):
            logger.warning("No available agents for session %s", session.session_id)
            if is_inbound_direct:
                await self._set_result(session, "disconnected", force=True, report=True)
                await self._hangup(session)
            return
        else:
            endpoint = (
                self.settings.operator.endpoint
                or f"PJSIP/{self.settings.operator.extension}@{self.settings.operator.trunk}"
            )

        app_args = f"operator,{session.session_id},{endpoint}"
        async with session.lock:
            session.metadata["operator_endpoint"] = endpoint
            if operator_mobile:
                session.metadata["operator_mobile"] = operator_mobile
                session.metadata["operator_outbound_line"] = outbound_line
                session.metadata["operator_agent_id"] = str(agent.get("id")) if agent and agent.get("id") else ""
            caller_id = (
                self.dialer._caller_id_for_line(outbound_line) if self.dialer and outbound_line
                else self.settings.operator.caller_id
            )
            if session.metadata.get("hungup") == "1":
                return

        logger.info("Connecting session %s to operator %s", session.session_id, endpoint)
        try:
            await self.ari_client.originate_call(
                endpoint=endpoint,
                app_args=app_args,
                caller_id=caller_id,
                timeout=self.settings.operator.timeout,
            )
            if operator_mobile:
                self.agent_busy.add(operator_mobile)
        except Exception as exc:
            async with session.lock:
                session.result = "failed:operator_failed"
                if outbound_line:
                    session.metadata.pop("operator_outbound_line", None)
            logger.exception("Operator originate failed for session %s: %s", session.session_id, exc)
            if outbound_line:
                await self._release_outbound_line(outbound_line)
            # Play goodbye if we have a scenario
            scenario = self._get_scenario(session)
            if scenario and "goodbye" in scenario.prompts:
                await self._play_prompt(session, "goodbye", scenario)
            elif scenario and "goodby" in scenario.prompts:
                await self._play_prompt(session, "goodby", scenario)

    async def _retry_operator_mobile(self, session: Session, reason: str) -> bool:
        async with session.lock:
            tried = set((session.metadata.get("operator_tried") or "").split(",")) if session.metadata.get("operator_tried") else set()
            current_mobile = session.metadata.get("operator_mobile")
            outbound_line = session.metadata.get("operator_outbound_line")
        if current_mobile:
            self.agent_busy.discard(current_mobile)
            tried.add(current_mobile)
        if outbound_line:
            await self._release_outbound_line(outbound_line)

        # Determine agent type from session metadata
        agent_type = "inbound" if session.metadata.get("inbound_direct") == "1" else "outbound"
        agent = self._next_available_agent(agent_type)
        while agent and agent["phone_number"] in tried:
            agent = self._next_available_agent(agent_type)
        if not agent:
            logger.warning("Operator retry: no agents for session %s", session.session_id)
            await self._set_result(session, "disconnected", force=True, report=True)
            await self._hangup(session)
            return False

        outbound_line = await self._reserve_outbound_line()
        if not outbound_line:
            logger.warning("Operator retry: no line for session %s", session.session_id)
            await self._set_result(session, "disconnected", force=True, report=True)
            await self._hangup(session)
            return False

        next_mobile = agent["phone_number"]
        endpoint = f"PJSIP/{next_mobile}@{self.settings.dialer.outbound_trunk}"
        app_args = f"operator,{session.session_id},{endpoint}"
        caller_id = self.dialer._caller_id_for_line(outbound_line) if self.dialer else self.settings.operator.caller_id
        async with session.lock:
            session.metadata["operator_mobile"] = next_mobile
            session.metadata["operator_outbound_line"] = outbound_line
            session.metadata["operator_endpoint"] = endpoint
            session.metadata["operator_tried"] = ",".join(tried | {next_mobile})
            session.metadata["operator_agent_id"] = str(agent.get("id")) if agent.get("id") else ""
        logger.info("Retrying operator for session %s via %s", session.session_id, endpoint)
        try:
            await self.ari_client.originate_call(
                endpoint=endpoint, app_args=app_args,
                caller_id=caller_id, timeout=self.settings.operator.timeout,
            )
            self.agent_busy.add(next_mobile)
            return True
        except Exception as exc:
            logger.exception("Operator retry failed for session %s: %s", session.session_id, exc)
            await self._release_outbound_line(outbound_line)
            return False

    # -- Result reporting --------------------------------------------------

    async def _set_result(self, session: Session, value: str, force: bool = False, report: bool = False) -> None:
        updated = False
        async with session.lock:
            if force or session.result is None or session.result in {"user_didnt_answer", "missed"}:
                session.result = value
                updated = True
        if updated and report:
            await self._report_result(session)

    async def _report_result(self, session: Session) -> None:
        async with session.lock:
            # Ensure we only report once per session, even if result changes
            if session.metadata.get("result_reported"):
                logger.debug(
                    "[%s] Result already reported (previous: %s, current: %s), skipping duplicate",
                    session.session_id,
                    session.metadata.get("last_reported_result"),
                    session.result,
                )
                return

            payload = {
                "contact_number": session.metadata.get("contact_number"),
                "result": session.result,
                "responses": list(session.responses),
                "session_id": session.session_id,
                "scenario": session.metadata.get("scenario_name"),
            }

            # Mark as reported to prevent any future reports
            session.metadata["result_reported"] = True
            session.metadata["last_reported_result"] = session.result
        logger.info("Report payload: %s", payload)
        if self.dialer:
            await self.dialer.on_result(
                session.session_id, session.result,
                session.metadata.get("number_id"),
                session.metadata.get("contact_number"),
                session.metadata.get("batch_id"),
                session.metadata.get("attempted_at"),
            )
        if self.panel_client:
            await self._report_to_panel(session)

    async def _hangup(self, session: Session) -> None:
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            return
        async with session.lock:
            session.metadata["app_hangup"] = "1"
        try:
            await self.ari_client.hangup_channel(channel_id)
        except Exception as exc:
            msg = ("Hangup failed for session %s: %s", session.session_id, exc)
            if "404" in str(exc):
                logger.debug(*msg)
            else:
                logger.warning(*msg)

    async def _report_to_panel(self, session: Session) -> None:
        if not self.panel_client:
            return
        number_id = session.metadata.get("number_id")
        phone_number = session.metadata.get("contact_number")
        if number_id is None and not phone_number:
            return
        batch_id = session.metadata.get("batch_id")
        attempted_iso = session.metadata.get("attempted_at")
        attempted_at = datetime.utcnow().replace(tzinfo=timezone.utc)
        if attempted_iso:
            try:
                attempted_at = datetime.fromisoformat(attempted_iso).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        result = session.result or "unknown"
        user_message = None
        if session.responses:
            user_message = session.responses[-1].get("text")

        status, reason = self._map_result_to_panel(result, session)

        async with session.lock:
            last_status = session.metadata.get("panel_last_status")
            if last_status == status:
                return
            session.metadata["panel_last_status"] = status

        scenario_name = session.metadata.get("scenario_name")
        panel_scenario_name = self.registry.get_panel_name(scenario_name)
        outbound_line = session.metadata.get("outbound_line")

        await self.panel_client.report_result(
            number_id=number_id,
            phone_number=phone_number,
            status=status,
            reason=reason,
            attempted_at=attempted_at,
            batch_id=batch_id,
            agent_id=session.metadata.get("operator_agent_id"),
            agent_phone=session.metadata.get("operator_mobile"),
            user_message=user_message if status in {"UNKNOWN", "DISCONNECTED", "CONNECTED", "NOT_INTERESTED", "INBOUND_CALL"} else None,
            scenario=panel_scenario_name,
            outbound_line=outbound_line,
        )

    def _map_result_to_panel(self, result: str, session: Session) -> tuple[str, str]:
        """Map internal result code to panel status + reason."""
        is_inbound_direct = session.metadata.get("inbound_direct") == "1"
        if result == "connected_to_operator":
            return "CONNECTED", "User said yes"
        elif result == "inbound_call":
            return "INBOUND_CALL", "Inbound call connected to agent"
        elif result == "not_interested":
            return "NOT_INTERESTED", "User declined"
        elif result in {"missed", "user_didnt_answer"}:
            return "MISSED", "No answer/busy/unreachable"
        elif result == "hangup":
            return "HANGUP", "Caller hung up"
        elif result == "disconnected":
            if is_inbound_direct:
                return "INBOUND_CALL", "Inbound call connected to agent"
            return "DISCONNECTED", "Caller disconnected"
        elif result == "unknown":
            return "UNKNOWN", "Unknown intent"
        elif result.startswith("failed:stt_failure"):
            return "NOT_INTERESTED", "User did not respond"
        elif result.startswith("failed:") or result == "failed":
            return "FAILED", result
        elif result == "busy":
            return "BUSY", "Line busy"
        elif result == "power_off":
            return "POWER_OFF", "Unavailable / powered off"
        elif result == "banned":
            return "BANNED", "Rejected by operator"
        return "FAILED", result

    # -- Utilities ---------------------------------------------------------

    def _is_empty_audio(self, audio_bytes: bytes) -> bool:
        if not audio_bytes or len(audio_bytes) < 800:
            return True
        try:
            with wave.open(io.BytesIO(audio_bytes), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                sampwidth = w.getsampwidth() or 2
                data = w.readframes(frames)
            duration = frames / rate if rate else 0
            rms = audioop.rms(data, sampwidth) if frames else 0
            max_amp = 2 ** (8 * sampwidth - 1)
            norm = rms / max_amp if max_amp else 0
            return duration < 0.1 or norm < 0.001
        except Exception:
            return False

    def _build_log(self, name: str, filename: str) -> logging.Logger:
        lg = logging.getLogger(name)
        if not lg.handlers:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            handler = RotatingFileHandler(log_dir / filename, maxBytes=2 * 1024 * 1024, backupCount=3)
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            handler.setFormatter(formatter)
            lg.addHandler(handler)
            lg.setLevel(logging.INFO)
            lg.propagate = False
        return lg
