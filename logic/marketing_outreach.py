import asyncio
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable, Optional

from config.settings import Settings
from core.ari_client import AriClient
from integrations.panel.client import PanelClient
from llm.client import GapGPTClient
from logic.base import BaseScenario
from sessions.session import CallLeg, LegDirection, Session
from sessions.session_manager import SessionManager
from stt_tts.vira_stt import STTResult, ViraSTTClient


logger = logging.getLogger(__name__)


class MarketingScenario(BaseScenario):
    """
    Outbound marketing flow:
    1) Play hello prompt.
    2) Capture single response. NO/unknown -> goodbye. YES -> play yes prompt then bridge to operator.
    3) If asked "شماره منو از کجا آوردید": play number prompt then goodbye.
    4) After bridge, let operator/caller hang up; then report status.
    """

    def __init__(
        self,
        settings: Settings,
        ari_client: AriClient,
        llm_client: GapGPTClient,
        stt_client: ViraSTTClient,
        session_manager: SessionManager,
        panel_client: Optional[PanelClient] = None,
    ):
        self.settings = settings
        self.ari_client = ari_client
        self.llm_client = llm_client
        self.stt_client = stt_client
        self.session_manager = session_manager
        self.panel_client = panel_client
        self.dialer = None
        # Audio prompts expected on Asterisk as converted prompts (wav/slin) under sounds/custom
        self.prompt_media = {
            "hello": "sound:custom/hello",
            "goodby": "sound:custom/goodby",
            "yes": "sound:custom/yes",
            "onhold": "sound:custom/onhold",
            "number": "sound:custom/number",
            "processing": "sound:beep",
        }
        # Hotwords to bias STT toward common intents (single words, no phrases).
        self.stt_hotwords = [
            # Negative/stop tokens
            "نه", "نیاز", "ندارم", "نمیخواهم", "نمیخوام", "ممنون", "ساعت", "زنگ",
            "وقت", "خصوصی", "شماره", "پاک", "حذف", "بچه", "کسی", "بفرستید",
            "دیگه", "تماس", "سرکار", "مدرس",
            # Positive/engaged tokens
            "بله", "اوکی", "خدمت", "بفرمایید", "تضمین", "کجا", "قیمت", "سایت",
            "نمونه", "تدریس", "آدرس", "ترم", "اساتید", "دوره", "کتاب", "پایه",
            "هزینه", "سازمان", "مدرک", "سطح", "مهاجرت", "توضیح", "باشه",
            "دیگه", "بزن","میشه", "برام","دارید", "کنید","با", "من", "نگیرید",
            "الان", "سرکارم","خودم", "مدرسم","می‌خوام","خواست", "رو", "میدم",
            "در", "خدمتم","چیه","هستید","قیمتش", "چنده","دارین","کجاست","ترمیکه",
            "کین","طول", "چقدره","چه", "میشه","شروع", "کنم","نظر","میدید","از","معتبره",
            "کنم","حالا", "شما", "یه", "بدید"
        ]
        self.negative_logger = self._build_negative_logger()

    def attach_dialer(self, dialer) -> None:
        self.dialer = dialer

    async def on_outbound_channel_created(self, session: Session) -> None:
        logger.debug("Outbound channel ready for session %s", session.session_id)

    async def on_inbound_channel_created(self, session: Session) -> None:
        logger.debug("Inbound call entered session %s; outbound scenario not used", session.session_id)

    async def on_operator_channel_created(self, session: Session) -> None:
        logger.debug("Operator leg created for session %s", session.session_id)

    async def on_call_answered(self, session: Session, leg: CallLeg) -> None:
        if leg.direction == LegDirection.OPERATOR:
            async with session.lock:
                session.result = session.result or "connected_to_operator"
                session.metadata["operator_connected"] = "1"
            await self._stop_onhold_playbacks(session)
            logger.info("Operator leg answered for session %s", session.session_id)
            return

        logger.info("Call answered for session %s (customer)", session.session_id)
        await self._play_prompt(session, "hello")

    async def on_playback_finished(self, session: Session, playback_id: str) -> None:
        async with session.lock:
            prompt_key = session.playbacks.pop(playback_id, None)
        if not prompt_key:
            return
        logger.debug("Playback %s finished for session %s (%s)", playback_id, session.session_id, prompt_key)

        if prompt_key == "hello":
            await self._capture_response(
                session,
                phase="interest",
                on_yes=self._handle_yes,
                on_no=self._handle_no,
            )
        elif prompt_key == "yes":
            # Operator connect may already be in progress from _handle_yes
            await self._play_onhold(session)
        elif prompt_key == "number":
            await self._capture_response(
                session,
                phase="number_followup",
                on_yes=self._handle_yes,
                on_no=self._handle_no,
            )
        elif prompt_key == "onhold":
            operator_connected = False
            async with session.lock:
                operator_connected = session.metadata.get("operator_connected") == "1"
            if not operator_connected:
                await self._play_onhold(session)
        elif prompt_key == "goodby":
            await self._hangup(session)

    async def on_call_failed(self, session: Session, reason: str) -> None:
        # Customer leg failed/busy/unanswered => missed
        result_value = "missed"
        await self._set_result(session, result_value, force=True, report=True)
        logger.warning("Call failed session=%s reason=%s", session.session_id, reason)
        await self._hangup(session)

    async def on_call_hangup(self, session: Session) -> None:
        operator_connected = False
        yes_intent = False
        no_intent = False
        app_hangup = False
        async with session.lock:
            operator_connected = session.metadata.get("operator_connected") == "1"
            yes_intent = session.metadata.get("intent_yes") == "1"
            no_intent = session.metadata.get("intent_no") == "1"
            app_hangup = session.metadata.get("app_hangup") == "1"
        if operator_connected:
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
        logger.debug("Call hangup signaled for session %s", session.session_id)

    async def on_call_finished(self, session: Session) -> None:
        async with session.lock:
            if session.result is None:
                session.result = "user_didnt_answer"
            result = session.result
        logger.info("Call finished session=%s result=%s", session.session_id, result)
        await self._report_result(session)
        if self.dialer:
            await self.dialer.on_session_completed(session.session_id)

    # Prompt handling -----------------------------------------------------
    async def _play_prompt(self, session: Session, prompt_key: str) -> None:
        media = self.prompt_media[prompt_key]
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No customer channel available to play %s for session %s", prompt_key, session.session_id)
            return
        playback = await self.ari_client.play_on_channel(channel_id, media)
        playback_id = playback.get("id")
        if playback_id:
            async with session.lock:
                session.playbacks[playback_id] = prompt_key
            await self.session_manager.register_playback(session.session_id, playback_id)
        logger.info("Playing prompt %s on channel %s", prompt_key, channel_id)

    async def _play_onhold(self, session: Session) -> None:
        # Start/loop hold music until operator answers.
        await self._play_prompt(session, "onhold")

    async def _stop_onhold_playbacks(self, session: Session) -> None:
        async with session.lock:
            hold_playbacks = [pb_id for pb_id, key in session.playbacks.items() if key == "onhold"]
        for pb_id in hold_playbacks:
            try:
                await self.ari_client.stop_playback(pb_id)
            except Exception as exc:
                logger.debug("Failed to stop onhold playback %s: %s", pb_id, exc)

    def _customer_channel_id(self, session: Session) -> Optional[str]:
        if session.outbound_leg:
            return session.outbound_leg.channel_id
        if session.inbound_leg:
            return session.inbound_leg.channel_id
        return None

    # Response capture ----------------------------------------------------
    async def _capture_response(
        self,
        session: Session,
        phase: str,
        on_yes: Callable[[Session], Awaitable[None]],
        on_no: Callable[[Session], Awaitable[None]],
    ) -> None:
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No channel to capture response for session %s", session.session_id)
            return

        recording_name = f"{phase}-{session.session_id}"
        async with session.lock:
            session.metadata["recording_phase"] = phase
            session.metadata["recording_name"] = recording_name
        logger.info("Recording %s response for session %s", phase, session.session_id)
        try:
            if session.bridge and session.bridge.bridge_id:
                await self.ari_client.record_bridge(
                    bridge_id=session.bridge.bridge_id,
                    name=recording_name,
                    max_duration=10,
                    max_silence=2,
                )
            else:
                await self.ari_client.record_channel(
                    channel_id=channel_id,
                    name=recording_name,
                    max_duration=10,
                    max_silence=2,
                )
            await self.session_manager.register_recording(session.session_id, recording_name)
        except Exception as exc:
            logger.exception("Failed to start recording (%s) for session %s: %s", phase, session.session_id, exc)
            await self._handle_no_response(session, phase, on_yes, on_no, reason="recording_failed")

    async def on_recording_finished(self, session: Session, recording_name: str) -> None:
        async with session.lock:
            phase = session.metadata.get("recording_phase")
            if not phase or session.metadata.get("recording_name") != recording_name:
                return
            if recording_name in session.processed_recordings:
                return
            session.processed_recordings.add(recording_name)
        on_yes, on_no = self._callbacks_for_phase(phase)
        asyncio.create_task(
            self._transcribe_response(session, recording_name, phase, on_yes, on_no)
        )

    async def on_recording_failed(self, session: Session, recording_name: str, cause: str) -> None:
        async with session.lock:
            phase = session.metadata.get("recording_phase")
            if not phase or session.metadata.get("recording_name") != recording_name:
                return
            if recording_name in session.processed_recordings:
                return
            session.processed_recordings.add(recording_name)
        on_yes, on_no = self._callbacks_for_phase(phase)
        logger.warning(
            "Recording failed (phase=%s) for session %s cause=%s", phase, session.session_id, cause
        )
        await self._handle_no_response(session, phase, on_yes, on_no, reason=f"recording_failed:{cause}")

    async def _transcribe_response(
        self,
        session: Session,
        recording_name: str,
        phase: str,
        on_yes: Callable[[Session], Awaitable[None]],
        on_no: Callable[[Session], Awaitable[None]],
    ) -> None:
        try:
            audio_bytes = await self.ari_client.fetch_stored_recording(recording_name)
            stt_result: STTResult = await self.stt_client.transcribe_audio(
                audio_bytes, hotwords=self.stt_hotwords
            )
            transcript = stt_result.text.strip()
            logger.info(
                "STT result (%s) for session %s: %s (status=%s)",
                phase,
                session.session_id,
                transcript,
                stt_result.status,
            )
            if not transcript:
                await self._handle_no_response(session, phase, on_yes, on_no, reason="empty_transcript")
                return
            intent = await self._detect_intent(transcript)
            async with session.lock:
                session.responses.append({"phase": phase, "text": transcript, "intent": intent})
            if intent == "number_question":
                await self._handle_number_question(session)
            elif intent == "yes":
                await on_yes(session)
            elif intent == "no":
                self._log_negative(session, transcript, phase)
                await on_no(session)
            else:
                await self._handle_no_response(session, phase, on_yes, on_no, reason="intent_unknown")
        except Exception as exc:
            logger.exception("Transcription failed (%s) for session %s: %s", phase, session.session_id, exc)
            await self._handle_no_response(session, phase, on_yes, on_no, reason="stt_failure")

    async def _detect_intent(self, transcript: str) -> str:
        text = transcript.lower()
        # Persian/English positive cues
        yes_tokens = {
            "yes", "sure", "ok", "yah", "yea", "yeah",
            "بله", "اوکی", "در خدمتم", "بفرمایید", "تضمین چیه", "کجا هستید", "قیمتش چنده",
            "سایت دارین", "نمونه تدریس", "آدرس کجاست", "ترمیکه", "اساتید کین",
            "طول دوره چقدره", "چه کتابی تدریس میشه", "من از پایه می‌خوام شروع کنم",
            "هزینه اش چقدره", "زیر نظر چه سازمانی هستید", "مدرک میدید", "از چه سطحی شروع میشه",
            "مدرک معتبره", "من می‌خوام مهاجرت کنم", "حالا شما یه توضیح بدید",
        }
        no_tokens = {
            "no", "not", "nope",
            "نه", "نیاز ندارم", "نمیخواهم", "ممنون", "دو ساعت دیگه زنگ بزن", "وقت ندارم",
            "میشه برام بفرستید", "خصوصی دارید", "شماره موپاک کنید", "دیگه بامن تماس نگیرید",
            "الان سرکارم", "خودم مدرسم", "شماره مو حذف کنید", "برای بچه ام می‌خوام",
            "برای کسی دیگه می‌خوام", "بفرستید کسی دیگه خواست شماره تون رو میدم",
        }
        number_question_phrases = [
            "شماره منو از کجا آوردی",
            "شماره منو از کجا آوردین",
            "شماره من را از کجا آوردی",
            "شماره را از کجا آوردید",
            "شماره از کجا آوردی",
            "شماره از کجا آوردید",
            "از کجا آوردی",
            "از کجا آوردین",
            "کجا آوردی",
            "کجا آوردید",
        ]
        if any(phrase in text for phrase in number_question_phrases):
            return "number_question"

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
                result = await self.llm_client.chat(
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
    async def _set_result(self, session: Session, value: str, force: bool = False, report: bool = False) -> None:
        updated = False
        async with session.lock:
            if force or session.result is None or session.result in {"user_didnt_answer", "missed"}:
                session.result = value
                updated = True
        if updated and report:
            await self._report_result(session)

    async def _handle_yes(self, session: Session) -> None:
        async with session.lock:
            session.metadata["intent_yes"] = "1"
        await self._play_prompt(session, "yes")
        # Start hold music and connect operator immediately (do not wait for playback finished).
        await self._play_onhold(session)
        await self._connect_to_operator(session)

    async def _handle_no(self, session: Session) -> None:
        async with session.lock:
            session.metadata["intent_no"] = "1"
        await self._set_result(session, "not_interested", force=True, report=True)
        await self._play_prompt(session, "goodby")

    async def _handle_no_response(
        self,
        session: Session,
        phase: str,
        on_yes: Callable[[Session], Awaitable[None]],
        on_no: Callable[[Session], Awaitable[None]],
        reason: str,
    ) -> None:
        logger.info(
            "No usable response detected (phase=%s reason=%s) session=%s",
            phase,
            reason,
            session.session_id,
        )
        if "stt_failure" in reason or "recording_failed" in reason or "error" in reason or reason.startswith("failed"):
            await self._set_result(session, f"failed:{reason}", force=True, report=True)
        else:
            await self._set_result(session, "missed", force=False, report=True)
        await self._play_prompt(session, "goodby")

    async def _handle_number_question(self, session: Session) -> None:
        logger.info("Handling number source question for session %s", session.session_id)
        await self._play_prompt(session, "number")

    # Operator bridge -----------------------------------------------------
    async def _connect_to_operator(self, session: Session) -> None:
        async with session.lock:
            if session.metadata.get("operator_call_started") == "1":
                logger.debug("Operator call already started for session %s; skipping", session.session_id)
                return
            if self._is_inbound_only(session):
                logger.debug("Inbound-only session %s; skipping operator connect", session.session_id)
                return
            session.metadata["operator_call_started"] = "1"
        customer_channel = self._customer_channel_id(session)
        if not customer_channel:
            logger.warning("Cannot connect to operator; no customer channel for session %s", session.session_id)
            return

        endpoint = f"PJSIP/{self.settings.operator.extension}@{self.settings.operator.trunk}"
        app_args = f"operator,{session.session_id},{endpoint}"
        async with session.lock:
            session.metadata["operator_endpoint"] = endpoint
        logger.info("Connecting session %s to operator endpoint %s", session.session_id, endpoint)
        try:
            await self.ari_client.originate_call(
                endpoint=endpoint,
                app_args=app_args,
                caller_id=self.settings.operator.caller_id,
                timeout=self.settings.operator.timeout,
            )
        except Exception as exc:
            async with session.lock:
                session.result = "failed:operator_failed"
            logger.exception("Operator originate failed for session %s: %s", session.session_id, exc)
            await self._play_prompt(session, "goodby")

    async def _play_processing(self, session: Session) -> None:
        """
        Play a quick acknowledgement (beep) to signal we are analyzing.
        """
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            return
        playback = await self.ari_client.play_on_channel(channel_id, self.prompt_media["processing"])
        playback_id = playback.get("id")
        if playback_id:
            async with session.lock:
                session.playbacks[playback_id] = "processing"
            await self.session_manager.register_playback(session.session_id, playback_id)

    def _callbacks_for_phase(self, phase: str) -> tuple[Callable[[Session], Awaitable[None]], Callable[[Session], Awaitable[None]]]:
        return self._handle_yes, self._handle_no

    def _is_inbound_only(self, session: Session) -> bool:
        return session.inbound_leg is not None and session.outbound_leg is None

    # Result reporting ----------------------------------------------------
    async def _report_result(self, session: Session) -> None:
        async with session.lock:
            payload = {
                "contact_number": session.metadata.get("contact_number"),
                "result": session.result,
                "responses": list(session.responses),
                "session_id": session.session_id,
            }
        logger.info("Report payload (stub): %s", payload)
        if self.dialer:
            await self.dialer.on_result(
                session.session_id,
                session.result,
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
            logger.warning("Hangup failed for session %s: %s", session.session_id, exc)

    def _build_negative_logger(self) -> logging.Logger:
        neg_logger = logging.getLogger("logic.negatives")
        if not neg_logger.handlers:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            handler = RotatingFileHandler(log_dir / "negative_stt.log", maxBytes=2 * 1024 * 1024, backupCount=3)
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            handler.setFormatter(formatter)
            neg_logger.addHandler(handler)
            neg_logger.setLevel(logging.INFO)
            neg_logger.propagate = False
        return neg_logger

    def _log_negative(self, session: Session, transcript: str, phase: str) -> None:
        self.negative_logger.info(
            "session=%s phase=%s transcript=%s", session.session_id, phase, transcript
        )

    async def _report_to_panel(self, session: Session) -> None:
        if not self.panel_client:
            return
        if session.inbound_leg and session.outbound_leg is None:
            return
        number_id = session.metadata.get("number_id")
        phone_number = session.metadata.get("contact_number")
        if number_id is None and not phone_number:
            logger.debug("Skipping panel report for session %s: no number_id/phone_number", session.session_id)
            return
        batch_id = session.metadata.get("batch_id")
        attempted_iso = session.metadata.get("attempted_at")
        from datetime import datetime, timezone

        attempted_at = datetime.utcnow().replace(tzinfo=timezone.utc)
        if attempted_iso:
            try:
                attempted_at = datetime.fromisoformat(attempted_iso).replace(tzinfo=timezone.utc)
            except Exception:
                pass

        result = session.result or "unknown"
        status = "FAILED"
        reason = result
        if result == "connected_to_operator":
            status = "CONNECTED"
            reason = "User said yes and connected to operator"
        elif result == "not_interested":
            status = "NOT_INTERESTED"
            reason = "User declined"
        elif result in {"missed", "user_didnt_answer"}:
            status = "MISSED"
            reason = "No answer/busy/unreachable"
        elif result == "hangup":
            status = "HANGUP"
            reason = "Caller hung up"
        elif result == "disconnected":
            status = "DISCONNECTED"
            reason = "Caller said yes but disconnected before operator answered"
        elif result.startswith("failed:") or result == "failed":
            status = "FAILED"
            reason = result

        # Avoid duplicate reports with the same status to panel.
        async with session.lock:
            last_status = session.metadata.get("panel_last_status")
            if last_status == status:
                return
            session.metadata["panel_last_status"] = status

        await self.panel_client.report_result(
            number_id=number_id,
            phone_number=phone_number,
            status=status,
            reason=reason,
            attempted_at=attempted_at,
            batch_id=batch_id,
        )
