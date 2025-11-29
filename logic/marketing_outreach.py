import logging
import threading
import time
from typing import Callable, Optional

from config.settings import Settings
from core.ari_client import AriClient
from llm.client import GapGPTClient
from logic.base import BaseScenario
from sessions.session import CallLeg, LegDirection, Session
from sessions.session_manager import SessionManager
from stt_tts.vira_stt import STTResult, transcribe_audio


logger = logging.getLogger(__name__)


class MarketingScenario(BaseScenario):
    """
    Outbound marketing flow:
    1) Play hello prompt.
    2) Capture YES/NO once. NO/unknown -> goodbye. YES -> play second prompt.
    3) Capture YES/NO once. YES -> bridge to operator ext (200). NO/unknown -> goodbye.
    4) After bridge, let operator/caller hang up; then report status.
    """

    def __init__(
        self,
        settings: Settings,
        ari_client: AriClient,
        llm_client: GapGPTClient,
        session_manager: SessionManager,
    ):
        self.settings = settings
        self.ari_client = ari_client
        self.llm_client = llm_client
        self.session_manager = session_manager
        self.dialer = None
        # Audio prompts expected on Asterisk as converted prompts (wav/slin) under sounds/custom
        self.prompt_media = {
            "hello": "sound:custom/hello",
            "goodby": "sound:custom/goodby",
            "second": "sound:custom/second",
            "processing": "sound:beep",
        }

    def attach_dialer(self, dialer) -> None:
        self.dialer = dialer

    def on_outbound_channel_created(self, session: Session) -> None:
        logger.debug("Outbound channel ready for session %s", session.session_id)

    def on_inbound_channel_created(self, session: Session) -> None:
        logger.debug("Inbound call entered session %s; outbound scenario not used", session.session_id)

    def on_operator_channel_created(self, session: Session) -> None:
        logger.debug("Operator leg created for session %s", session.session_id)

    def on_call_answered(self, session: Session, leg: CallLeg) -> None:
        if leg.direction == LegDirection.OPERATOR:
            session.result = session.result or "connected_to_operator"
            logger.info("Operator leg answered for session %s", session.session_id)
            return

        # Customer leg answered
        logger.info("Call answered for session %s (customer)", session.session_id)
        self._play_prompt(session, "hello")

    def on_playback_finished(self, session: Session, playback_id: str) -> None:
        prompt_key = session.playbacks.pop(playback_id, None)
        if not prompt_key:
            return
        logger.debug("Playback %s finished for session %s (%s)", playback_id, session.session_id, prompt_key)

        if prompt_key == "hello":
            self._capture_response(session, phase="interest", on_yes=self._handle_interest_yes, on_no=self._handle_interest_no)
        elif prompt_key == "second":
            self._capture_response(session, phase="confirm_transfer", on_yes=self._handle_confirm_yes, on_no=self._handle_confirm_no)
        elif prompt_key == "goodby":
            self._hangup(session)
        elif prompt_key == "processing":
            return

    def on_call_failed(self, session: Session, reason: str) -> None:
        if session.result is None:
            session.result = f"failed:{reason}"
        logger.warning("Call failed session=%s reason=%s", session.session_id, reason)
        self._hangup(session)

    def on_call_hangup(self, session: Session) -> None:
        logger.debug("Call hangup signaled for session %s", session.session_id)

    def on_call_finished(self, session: Session) -> None:
        if session.result is None:
            session.result = "user_didnt_answer"
        logger.info("Call finished session=%s result=%s", session.session_id, session.result)
        self._report_result(session)
        if self.dialer:
            self.dialer.on_session_completed(session.session_id)

    # Prompt handling -----------------------------------------------------
    def _play_prompt(self, session: Session, prompt_key: str) -> None:
        media = self.prompt_media[prompt_key]
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No customer channel available to play %s for session %s", prompt_key, session.session_id)
            return
        playback = self.ari_client.play_on_channel(channel_id, media)
        playback_id = playback.get("id")
        if playback_id:
            session.playbacks[playback_id] = prompt_key
            self.session_manager.register_playback(session.session_id, playback_id)
        logger.info("Playing prompt %s on channel %s", prompt_key, channel_id)

    def _customer_channel_id(self, session: Session) -> Optional[str]:
        if session.outbound_leg:
            return session.outbound_leg.channel_id
        if session.inbound_leg:
            return session.inbound_leg.channel_id
        return None

    # Response capture ----------------------------------------------------
    def _capture_response(
        self,
        session: Session,
        phase: str,
        on_yes: Callable[[Session], None],
        on_no: Callable[[Session], None],
    ) -> None:
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No channel to capture response for session %s", session.session_id)
            return

        recording_name = f"{phase}-{session.session_id}"
        session.metadata["recording_phase"] = phase
        session.metadata["recording_name"] = recording_name
        logger.info("Recording %s response for session %s", phase, session.session_id)
        try:
            self.ari_client.record_channel(
                channel_id=channel_id,
                name=recording_name,
                max_duration=10,
                max_silence=1,
            )
        except Exception as exc:
            logger.exception("Failed to start recording (%s) for session %s: %s", phase, session.session_id, exc)
            self._handle_no_response(session, phase, on_yes, on_no, reason="recording_failed")
            return

    def on_recording_finished(self, session: Session, recording_name: str) -> None:
        phase = session.metadata.get("recording_phase")
        if not phase or session.metadata.get("recording_name") != recording_name:
            return
        on_yes, on_no = self._callbacks_for_phase(phase)
        thread = threading.Thread(
            target=self._transcribe_response,
            args=(session, recording_name, phase, on_yes, on_no),
            daemon=True,
        )
        thread.start()

    def on_recording_failed(self, session: Session, recording_name: str, cause: str) -> None:
        phase = session.metadata.get("recording_phase")
        if not phase or session.metadata.get("recording_name") != recording_name:
            return
        on_yes, on_no = self._callbacks_for_phase(phase)
        logger.warning(
            "Recording failed (phase=%s) for session %s cause=%s", phase, session.session_id, cause
        )
        self._handle_no_response(session, phase, on_yes, on_no, reason=f"recording_failed:{cause}")

    def _transcribe_response(
        self,
        session: Session,
        recording_name: str,
        phase: str,
        on_yes: Callable[[Session], None],
        on_no: Callable[[Session], None],
    ) -> None:
        time.sleep(0.5)
        try:
            audio_bytes = self.ari_client.fetch_stored_recording(recording_name)
            self._play_processing(session)
            stt_result: STTResult = transcribe_audio(audio_bytes, self.settings.vira)
            transcript = stt_result.text.strip()
            logger.info(
                "STT result (%s) for session %s: %s (status=%s)",
                phase,
                session.session_id,
                transcript,
                stt_result.status,
            )
            if not transcript:
                self._handle_no_response(session, phase, on_yes, on_no, reason="empty_transcript")
                return
            intent = self._detect_yes_no(transcript)
            session.responses.append({"phase": phase, "text": transcript, "intent": intent})
            if intent == "yes":
                on_yes(session)
            elif intent == "no":
                on_no(session)
            else:
                self._handle_no_response(session, phase, on_yes, on_no, reason="intent_unknown")
        except Exception as exc:
            logger.exception("Transcription failed (%s) for session %s: %s", phase, session.session_id, exc)
            self._handle_no_response(session, phase, on_yes, on_no, reason="stt_failure")

    def _detect_yes_no(self, transcript: str) -> str:
        text = transcript.lower()
        yes_tokens = {"yes", "sure", "ok", "yah", "yea", "yeah", "بله", "اره", "آره", "مایلم"}
        no_tokens = {"no", "not", "nope", "نه", "خیر", "نیستم"}

        if any(token in text for token in yes_tokens):
            return "yes"
        if any(token in text for token in no_tokens):
            return "no"

        if self.llm_client.api_key:
            prompt = (
                "You are a fast classifier. Reply with only 'yes', 'no', or 'unknown'. "
                "Decide the intent based on this short utterance: "
                f"\"{transcript}\""
            )
            try:
                result = self.llm_client.chat(
                    messages=[{"role": "user", "content": prompt}],
                    model="gpt-4o-mini",
                    temperature=0,
                )
                normalized = result.strip().lower()
                if "yes" in normalized:
                    return "yes"
                if "no" in normalized:
                    return "no"
            except Exception as exc:
                logger.warning("LLM intent fallback failed: %s", exc)
        return "unknown"

    # Routing -------------------------------------------------------------
    def _handle_interest_yes(self, session: Session) -> None:
        session.result = session.result or "interested"
        self._play_prompt(session, "second")

    def _handle_interest_no(self, session: Session) -> None:
        session.result = "not_interested"
        self._play_prompt(session, "goodby")

    def _handle_confirm_yes(self, session: Session) -> None:
        session.result = "connected_to_operator"
        self._connect_to_operator(session)

    def _handle_confirm_no(self, session: Session) -> None:
        session.result = "not_interested"
        self._play_prompt(session, "goodby")

    def _handle_no_response(
        self,
        session: Session,
        phase: str,
        on_yes: Callable[[Session], None],
        on_no: Callable[[Session], None],
        reason: str,
    ) -> None:
        logger.info(
            "No usable response detected (phase=%s reason=%s) session=%s",
            phase,
            reason,
            session.session_id,
        )
        session.result = session.result or "user_didnt_answer"
        self._play_prompt(session, "goodby")

    # Operator bridge -----------------------------------------------------
    def _connect_to_operator(self, session: Session) -> None:
        if session.metadata.get("operator_call_started") == "1":
            logger.debug("Operator call already started for session %s; skipping", session.session_id)
            return
        customer_channel = self._customer_channel_id(session)
        if not customer_channel:
            logger.warning("Cannot connect to operator; no customer channel for session %s", session.session_id)
            return

        endpoint = f"PJSIP/{self.settings.operator.extension}@{self.settings.operator.trunk}"
        app_args = f"operator,{session.session_id},{endpoint}"
        session.metadata["operator_endpoint"] = endpoint
        session.metadata["operator_call_started"] = "1"
        logger.info("Connecting session %s to operator endpoint %s", session.session_id, endpoint)
        try:
            self.ari_client.originate_call(
                endpoint=endpoint,
                app_args=app_args,
                caller_id=self.settings.operator.caller_id,
                timeout=self.settings.operator.timeout,
            )
        except Exception as exc:
            session.result = "failed:operator_failed"
            logger.exception("Operator originate failed for session %s: %s", session.session_id, exc)
            self._play_prompt(session, "goodby")

    def _play_processing(self, session: Session) -> None:
        """
        Play a quick acknowledgement (beep) to signal we are analyzing.
        """
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            return
        playback = self.ari_client.play_on_channel(channel_id, self.prompt_media["processing"])
        playback_id = playback.get("id")
        if playback_id:
            session.playbacks[playback_id] = "processing"
            self.session_manager.register_playback(session.session_id, playback_id)

    def _callbacks_for_phase(self, phase: str) -> tuple[Callable[[Session], None], Callable[[Session], None]]:
        if phase == "interest":
            return self._handle_interest_yes, self._handle_interest_no
        return self._handle_confirm_yes, self._handle_confirm_no

    # Result reporting ----------------------------------------------------
    def _report_result(self, session: Session) -> None:
        payload = {
            "contact_number": session.metadata.get("contact_number"),
            "result": session.result,
            "responses": session.responses,
            "session_id": session.session_id,
        }
        logger.info("Report payload (stub): %s", payload)
        # TODO: integrate with external panel API when available.

    def _hangup(self, session: Session) -> None:
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            return
        try:
            self.ari_client.hangup_channel(channel_id)
        except Exception as exc:
            logger.warning("Hangup failed for session %s: %s", session.session_id, exc)
