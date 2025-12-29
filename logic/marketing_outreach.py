import asyncio
import logging
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Awaitable, Callable, Optional
import io
import wave
import audioop
import httpx

from config.settings import Settings
from core.ari_client import AriClient
from integrations.panel.client import PanelClient
from llm.client import GapGPTClient
from logic.base import BaseScenario
from sessions.session import CallLeg, LegDirection, LegState, Session
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
        # Agent mobiles (optional): round-robin, skip busy
        self.agent_mobiles = [m for m in settings.operator.mobile_numbers if m]
        self.agent_busy: set[str] = set()
        self.agent_cursor = 0
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
            # Negative / stop tokens
            "نه", "نیاز", "ندارم", "نمیخواهم", "نمیخوام", "ممنون", "ساعت", "زنگ",
            "وقت", "خصوصی", "شماره", "پاک", "حذف", "بچه", "کسی", "بفرستید",
            "دیگه", "تماس", "سرکار", "مدرس", "نگیرید",

            # Positive / engaged tokens
            "بله", "اوکی", "آره", "باشه",
            "در", "خدمت", "خدمتم", "بفرمایید",
            "تضمین", "کجا", "کجاست",
            "قیمت", "قیمتش", "چنده",
            "سایت", "دارین", "دارید",
            "نمونه", "تدریس",
            "آدرس",
            "ترم", "ترمیکه",
            "اساتید", "کین",
            "دوره", "طول", "چقدره",
            "کتاب",
            "پایه",
            "هزینه",
            "سازمان",
            "مدرک", "معتبره",
            "سطح",
            "مهاجرت",
            "توضیح", "بدید",
            "نظر", "میدید",
            "شروع", "کنم",
            "حالا", "شما",
            "می‌خوام",
            "وصل", "کنید",

            # نحوه برگزاری
            "برگزار", "چطوری",
            "آنلاین", "آنلاینه",
            "آفلاین", "افلاینه",
            "اپلیکیشن", "اپلیکیشنی",

            # آموزشگاه
            "آموزشگاه", "اسم",
            "فیلم", "آموزشی",

            # زبان‌ها و دوره‌ها
            "ایلتس", "مکالمه", "دکترا",
            "ترکی", "فرانسه", "آلمانی", "روسی", "چینی", "کره", "عربی",

            # زمان و سابقه
            "چند", "وقته", "هست",

            # اعتراض به شماره
            "آوردی", "آوردین", "آوردید", "را",

            # ضمایر و حروف پرکاربرد
            "من", "با", "تو", "برای", "رو", "تون",
            "از", "چه", "میشه", "بزن", "الان", "سرکارم", "خودم", "مدرسم",
            "خواست", "میدم"
        ]

        self.negative_logger = self._build_negative_logger()
        self.positive_logger = self._build_positive_logger()

    def attach_dialer(self, dialer) -> None:
        self.dialer = dialer

    def _next_available_agent(self) -> Optional[str]:
        if not self.agent_mobiles:
            return None
        n = len(self.agent_mobiles)
        for i in range(n):
            idx = (self.agent_cursor + i) % n
            mobile = self.agent_mobiles[idx]
            if mobile not in self.agent_busy:
                self.agent_cursor = (idx + 1) % n
                return mobile
        return None

    async def _reserve_outbound_line(self) -> Optional[str]:
        """Reuse dialer line selection and counters for operator/mobile legs."""
        if not self.dialer:
            return None
        line = self.dialer._available_line()  # reuse dialer logic
        if not line:
            return None
        async with self.dialer.lock:
            stats = self.dialer.line_stats.get(line)
            if stats is None:
                return None
            stats["active"] += 1
            stats["attempts"].append(datetime.utcnow())
            stats["daily"] += 1
        self.dialer._record_attempt()
        return line

    async def _release_outbound_line(self, line: Optional[str]) -> None:
        if not line or not self.dialer:
            return
        async with self.dialer.lock:
            stats = self.dialer.line_stats.get(line)
            if stats:
                stats["active"] = max(stats.get("active", 0) - 1, 0)

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

        async with session.lock:
            session.metadata["answered_at"] = str(time.time())
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
            await self._play_onhold(session)
            # Small delay so "yes" finishes cleanly before ringing operator
            await asyncio.sleep(0.5)
            await self._connect_to_operator(session)
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
        operator_failed = session.operator_leg and session.operator_leg.state == LegState.FAILED
        if operator_failed:
            await self._stop_onhold_playbacks(session)
            # If user said yes but operator failed, mark disconnected; otherwise hangup.
            async with session.lock:
                yes_intent = session.metadata.get("intent_yes") == "1"
            result_value = "disconnected" if yes_intent else "hangup"
            await self._set_result(session, result_value, force=True, report=True)
            await self._hangup(session)
            return
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
            session.metadata["hungup"] = "1"
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
        await self._stop_onhold_playbacks(session)

    async def on_call_finished(self, session: Session) -> None:
        async with session.lock:
            if session.result is None:
                session.result = "user_didnt_answer"
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

    # Prompt handling -----------------------------------------------------
    async def _play_prompt(self, session: Session, prompt_key: str) -> None:
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
        media = self.prompt_media[prompt_key]
        channel_id = self._customer_channel_id(session)
        if not channel_id:
            logger.warning("No customer channel available to play %s for session %s", prompt_key, session.session_id)
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
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
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
            if session.metadata.get("hungup") == "1":
                return
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
            if self._is_empty_audio(audio_bytes):
                logger.info(
                    "Recording deemed empty/too short; marking hangup session=%s phase=%s",
                    session.session_id,
                    phase,
                )
                await self._set_result(session, "hangup", force=True, report=True)
                return
            stt_result: STTResult = await self.stt_client.transcribe_audio(
                audio_bytes, hotwords=self.stt_hotwords
            )
            async with session.lock:
                if session.metadata.get("hungup") == "1":
                    return
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
                self._log_positive(session, transcript, phase)
                await on_yes(session)
            elif intent == "no":
                self._log_negative(session, transcript, phase)
                await on_no(session)
            else:
                await self._handle_no_response(session, phase, on_yes, on_no, reason="intent_unknown")
        except Exception as exc:
            logger.exception("Transcription failed (%s) for session %s: %s", phase, session.session_id, exc)
            # If Vira returned balance error, pause dialer and alert immediately.
            msg = str(exc)
            if "balanceError" in msg or "credit is below the set threshold" in msg:
                async with session.lock:
                    session.metadata["panel_last_status"] = "FAILED"
                if self.dialer:
                    await self.dialer.on_result(
                        session.session_id,
                        "failed:stt_balance",
                        session.metadata.get("number_id"),
                        session.metadata.get("contact_number"),
                        session.metadata.get("batch_id"),
                        session.metadata.get("attempted_at"),
                    )
            # If Vira says empty audio, treat as user hangup.
            if "Empty Audio file" in msg or "Input file content is unexpected" in msg:
                await self._set_result(session, "hangup", force=True, report=True)
                return
            await self._handle_no_response(session, phase, on_yes, on_no, reason="stt_failure")
            await self._handle_no_response(session, phase, on_yes, on_no, reason="stt_failure")

    async def _detect_intent(self, transcript: str) -> str:
        text = transcript.lower()
        # Persian/English positive cues
        yes_tokens = {
            "بله", "آره", "اوکی", "در خدمتم", "بفرمایید", "تضمین چیه", "کجا هستید", "قیمتش چنده",
            "سایت دارین", "نمونه تدریس", "آدرس کجاست", "ترمیکه", "اساتید کین",
            "طول دوره چقدره", "چه کتابی تدریس میشه", "من از پایه می‌خوام شروع کنم",
            "هزینه اش چقدره", "زیر نظر چه سازمانی هستید", "مدرک میدید", "از چه سطحی شروع میشه",
            "مدرک معتبره", "من می‌خوام مهاجرت کنم", "حالا شما یه توضیح بدید",
            "وصل کنید", "دوره ایلتس", "دوره مکالمه", "دوره دکترا",
            "ترکی", "فرانسه", "آلمانی", "روسی",
            "چینی", "کره ای", "عربی", "کجا برگزار میشه", "آموزشگاه کجاس", "چند وقته هست",
            "چطوری برگزار میشه", "تو چه اپلیکیشنی هست", "آنلاینه", "افلاینه", "اسم آموزشگاهتون",
            "سایت هم دارید", "نمونه فیلم آموزشی دارید",
            "می شه سایتتون رو برا من بفرستید ببینم لطفا",
            "عربی به چه لهجه ای",
            "برای دوره های زبان انگلیسی به چه شکله بله",
            "ممنونم از بله",
            "دوره های آلمان تون به چه صورت هست",
            "کلاس حضوری ندارید",
            "فرمودید سایت آموزشی",
            "آدرستون کجاست",
            "آموزشگاه کجاست", "میشه برام بفرستید",
            "آدرس دارید یا فقط غیرحضوریه",
            "می‌خواهد لینک/اطلاعات را بعدا ببیند (درخواست ارسال لینک/اطلاع)",
            "می‌پرسد درباره دوره‌های آلمانی یا فرانسه/زبان‌ها",
            "سوال می‌پرسد آموزشگاه کجاست یا کدام آموزشگاه هستید",
            "درخواست راهنمایی یا توضیح بیشتر درباره دوره",
            "سوال محل برگزاری برای حضور (کجا هستید برای حضور)",
            "تماس برگشتی برای اطلاع از کلاس‌ها بعد از میس‌کال",
        }
        no_tokens = {
            "نه", "نیاز ندارم", "نمیخواهم", "ممنون", "دو ساعت دیگه زنگ بزن", "وقت ندارم",
            "خصوصی دارید", "شماره موپاک کنید", "دیگه بامن تماس نگیرید",
            "الان سرکارم", "خودم مدرسم", "شماره مو حذف کنید", "برای بچه ام می‌خوام",
            "برای کسی دیگه می‌خوام", "بفرستید کسی دیگه خواست شماره تون رو میدم",
        }
        # Fast-path: if transcript already contains a clear yes token, skip LLM.
        for token in ("بله", "آره"):
            if token in transcript:
                return "yes"
        if self.llm_client.api_key:
            # Provide intent examples to the LLM so it understands what we treat as yes/no.
            positive_examples = list(yes_tokens)[:30]  # keep prompt concise
            negative_examples = list(no_tokens)[:20]
            number_q_examples = [
                "شماره منو از کجا آوردی",
                "شماره منو از کجا آوردین",
                "شماره من را از کجا آوردی",
                "شماره را از کجا آوردید",
                "شماره از کجا آوردی",
            ]
            prompt = (
                "Classify intent into one word: yes / no / number_question / unknown.\n"
                "YES = interest or any question about price/place/time/links/who/where/how, any language mentioned, requests for info.\n"
                "Examples YES: " + "; ".join(positive_examples) + ".\n"
                "NO = reject/decline/not interested. Examples NO: " + "; ".join(negative_examples) + ".\n"
                "NUMBER_QUESTION = asks where we got their number. Examples: " + "; ".join(number_q_examples) + ".\n"
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
                    await self._handle_llm_quota_error(session, exc)
        return "unknown"

    def _extract_intent_label(self, normalized: str) -> Optional[str]:
        """
        Parse LLM output into a clean intent label; avoid substring matches that misclassify.
        """
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
        """
        Detect GapGPT quota errors (e.g., pre_consume_token_quota_failed).
        """
        if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None:
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
        return "pre_consume_token_quota_failed" in msg or "token quota is not enough" in msg

    async def _handle_llm_quota_error(self, session: Session, exc: Exception) -> None:
        """
        Mirror Vira balance handling: mark failure so dialer pauses and SMS/panel alert can fire.
        """
        async with session.lock:
            session.metadata["panel_last_status"] = "FAILED"
        if not self.dialer:
            return
        await self.dialer.on_result(
            session.session_id,
            "failed:llm_quota",
            session.metadata.get("number_id"),
            session.metadata.get("contact_number"),
            session.metadata.get("batch_id"),
            session.metadata.get("attempted_at"),
        )

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
            if session.metadata.get("hungup") == "1":
                return
        # If customer leg is already gone, skip operator flow.
        if not self._customer_channel_id(session):
            logger.debug("Skipping yes handling; customer channel missing for session %s", session.session_id)
            return
        async with session.lock:
            session.metadata["intent_yes"] = "1"
            session.metadata["yes_at"] = str(time.time())
        await self._play_prompt(session, "yes")

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
        async with session.lock:
            if session.metadata.get("hungup") == "1":
                return
            # If we already have a result set (e.g., hangup), do not override to failed.
            if session.result and session.result not in {"user_didnt_answer", "missed"}:
                return
        logger.info(
            "No usable response detected (phase=%s reason=%s) session=%s",
            phase,
            reason,
            session.session_id,
        )
        if "intent_unknown" in reason or reason == "unknown":
            await self._set_result(session, "not_interested", force=True, report=True)
        elif "stt_failure" in reason or "recording_failed" in reason or "error" in reason or reason.startswith("failed"):
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
            if session.metadata.get("hungup") == "1":
                logger.debug("Skip operator connect; session %s already hung up", session.session_id)
                return
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

        # Choose operator endpoint: mobiles (round-robin) if provided, else static endpoint/extension.
        endpoint = ""
        operator_mobile = None
        outbound_line = None
        if self.agent_mobiles:
            operator_mobile = self._next_available_agent()
            if not operator_mobile:
                logger.warning("No available operator mobiles to connect session %s", session.session_id)
                return
            outbound_line = await self._reserve_outbound_line()
            if not outbound_line:
                logger.warning("No available outbound line to reach operator mobile for session %s", session.session_id)
                return
            endpoint = f"PJSIP/{operator_mobile}@{self.settings.dialer.outbound_trunk}"
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
            caller_id = session.metadata.get("contact_number") or (
                self.dialer._caller_id_for_line(outbound_line) if (operator_mobile and outbound_line and self.dialer) else self.settings.operator.caller_id
            )
            if session.metadata.get("hungup") == "1":
                logger.debug("Skip operator connect; session %s already hung up", session.session_id)
                return
        logger.info("Connecting session %s to operator endpoint %s", session.session_id, endpoint)
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
                # Release line reservation on failure
                if outbound_line:
                    session.metadata.pop("operator_outbound_line", None)
            logger.exception("Operator originate failed for session %s: %s", session.session_id, exc)
            if outbound_line:
                await self._release_outbound_line(outbound_line)
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
        # Inbound calls should follow the same flow (including operator transfer)
        # so never block operator connect for inbound-only sessions.
        return False

    # Result reporting ----------------------------------------------------
    async def _report_result(self, session: Session) -> None:
        async with session.lock:
            payload = {
                "contact_number": session.metadata.get("contact_number"),
                "result": session.result,
                "responses": list(session.responses),
                "session_id": session.session_id,
            }
            last_reported = session.metadata.get("last_reported_result")
            if last_reported == session.result:
                return
            session.metadata["last_reported_result"] = session.result
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
        # Report both outbound and inbound (inbound has no number_id; phone_number is used).
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

    def _build_positive_logger(self) -> logging.Logger:
        pos_logger = logging.getLogger("logic.positives")
        if not pos_logger.handlers:
            log_dir = Path("logs")
            log_dir.mkdir(exist_ok=True)
            handler = RotatingFileHandler(log_dir / "positive_stt.log", maxBytes=2 * 1024 * 1024, backupCount=3)
            formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
            handler.setFormatter(formatter)
            pos_logger.addHandler(handler)
            pos_logger.setLevel(logging.INFO)
            pos_logger.propagate = False
        return pos_logger

    def _log_positive(self, session: Session, transcript: str, phase: str) -> None:
        self.positive_logger.info(
            "session=%s phase=%s transcript=%s", session.session_id, phase, transcript
        )

    async def _report_to_panel(self, session: Session) -> None:
        if not self.panel_client:
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
        elif result == "unknown":
            status = "NOT_INTERESTED"
            reason = "Unknown intent"
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

    def _is_empty_audio(self, audio_bytes: bytes) -> bool:
        """
        Heuristic: treat as empty if duration <0.1s or normalized RMS < 0.001.
        Falls back to byte-length check if parsing fails.
        """
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
