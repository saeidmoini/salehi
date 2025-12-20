# Salehi ARI Call Engine

Outbound/inbound ARI-driven call-control engine for a language academy marketing campaign. The app originates calls from one trunk (multiple outbound lines supported), plays prompts, captures intent via STT+LLM, and enforces concurrency/rate limits.

## Features
- Bridge-centric ARI control (Asterisk 20 / FreePBX 17).
- Outbound dialer with per-line limits (concurrent/per-minute/per-day) and least-load line selection using `OUTBOUND_NUMBERS`; pulls batches from panel when `call_allowed=true`, or uses `STATIC_CONTACTS` if panel is disabled.
- Scenario logic for marketing outreach (hello → single capture → LLM classify yes/no/number_question; yes plays `yes` then bridges operator; no/unknown plays `goodby`; number_question plays `number` then one more capture). Inbound calls follow the same flow but do not report to panel.
- Operator leg presents the customer’s number as caller ID (fallback to `OPERATOR_CALLER_ID`).
- STT via Vira with ffmpeg pre-processing (denoise/normalize). Enhanced copies are saved under `/var/spool/asterisk/recording/enhanced/` for review. Positive/negative transcripts are logged (`logs/positive_stt.log`, `logs/negative_stt.log`).
- Optional GapGPT (gpt-4o-mini) for intent classification with guided examples.
- In-memory session manager ready for future Redis-backed storage.
- Async/await architecture (httpx + websockets) with semaphore-guarded STT/TTS/LLM calls and HTTP connection pooling.

## Quick Start
1. Install Python 3.12.
2. Install system dependency `ffmpeg` (for prompt conversion): e.g. `sudo apt-get update && sudo apt-get install -y ffmpeg`.
2. Create a venv: `python -m venv venv && source venv/bin/activate`
3. Install deps: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in ARI, trunk, tokens, and either panel creds or `STATIC_CONTACTS` for local testing (set `PANEL_*` empty to disable panel).
5. Ensure ARI dialplan sends calls to `Stasis(salehi)` and ARI user is configured.
6. Prompts live in-repo under `assets/audio/` (mp3 sources and 16 kHz mono wav). To install them on Asterisk run (with the right permissions) `bash scripts/sync_audio.sh` which copies wavs to `/var/lib/asterisk/sounds/custom/` as `hello`, `goodby`, `yes`, `number`, `onhold` (override target with `AST_SOUND_DIR`).
7. Run: `python main.py` (async entrypoint; startup auto-converts mp3→wav and syncs prompts to Asterisk).

Note: Ensure `AST_SOUND_DIR` points to your actual Asterisk custom sounds path (e.g., `/var/lib/asterisk/sounds/custom` or `/var/lib/asterisk/sounds/en/custom`). The app will try to sync to both base and `en/custom` when possible. If permissions block copying, run as a user with rights or pre-create the directories.

## Configuration
Set via environment or `.env`:
- ARI: `ARI_BASE_URL`, `ARI_WS_URL`, `ARI_APP_NAME`, `ARI_USERNAME`, `ARI_PASSWORD`
- Dialer/lines: `OUTBOUND_TRUNK`, `OUTBOUND_NUMBERS` (comma-separated lines), `DEFAULT_CALLER_ID`, `ORIGINATION_TIMEOUT`, `MAX_CONCURRENT_CALLS`, `MAX_CALLS_PER_MINUTE`, `MAX_CALLS_PER_DAY`, `DIALER_BATCH_SIZE`, `DIALER_DEFAULT_RETRY`
- Contacts: `STATIC_CONTACTS` (comma-separated) when panel is disabled
- Panel: `PANEL_BASE_URL`, `PANEL_API_TOKEN` (leave empty to disable panel)
- LLM: `GAPGPT_BASE_URL`, `GAPGPT_API_KEY` (optional; uses gpt-4o-mini)
- Vira: `VIRA_STT_TOKEN`, `VIRA_TTS_TOKEN`, `VIRA_STT_URL`, `VIRA_TTS_URL`
- Operator bridge: `OPERATOR_EXTENSION`, `OPERATOR_TRUNK`, `OPERATOR_CALLER_ID`, `OPERATOR_TIMEOUT`
- Concurrency/timeouts: `HTTP_MAX_CONNECTIONS`, `HTTP_TIMEOUT`, `ARI_TIMEOUT`, `STT_TIMEOUT`, `TTS_TIMEOUT`, `LLM_TIMEOUT`, `MAX_PARALLEL_STT`, `MAX_PARALLEL_TTS`, `MAX_PARALLEL_LLM`
- SMS alerts: `SMS_API_KEY`, `SMS_FROM`, `SMS_ADMINS`, `FAIL_ALERT_THRESHOLD` (pauses dialer and notifies after consecutive failures)
- Logging: `LOG_LEVEL`

## Architecture
- `main.py`: async entrypoint wiring settings, async ARI HTTP/WebSocket clients, session manager, dialer, and marketing scenario; runs under `asyncio.run`.
- `core/`: async ARI REST client (`ari_client.py`, httpx with pooling/timeouts) and WebSocket listener (`ari_ws.py`, websockets) that fans events into tasks.
- `sessions/`: async `SessionManager` (asyncio locks) that routes ARI events to scenario hooks and manages bridges.
- `logic/`: `dialer.py` for rate-limited origination (async loop) with optional panel batches; `marketing_outreach.py` for scenario logic; `base.py` for shared scenario hooks.
- `integrations/panel/`: async client for panel dialer API (next batch, report result).
- `llm/`: async GapGPT wrapper (`client.py`) with semaphore limits.
- `stt_tts/`: async Vira STT/TTS wrappers with semaphore limits.
- `config/`: env loader and strongly-typed settings, including concurrency/timeouts.

## Scenario Flow (current)
1. Dialer pulls numbers from panel batches when allowed (or `STATIC_CONTACTS` fallback when panel disabled) and originates via `PJSIP/<dialstring>@<OUTBOUND_TRUNK>` where dialstring = last 4 digits of the chosen line + customer digits; per-line limits and least-load selection apply.
2. On answer, play `hello`.
3. Record a short reply (10s max, 2s silence stop), transcribe with Vira STT (audio enhanced via ffmpeg), and classify intent via LLM (guided yes/no/number_question examples).
4. If intent is **no** or silence/unknown: play `goodby`, then hang up (negative transcripts also logged to `logs/negative_stt.log`).
5. If intent is **yes**: play `yes`, then originate/bridge operator leg to `PJSIP/<OPERATOR_EXTENSION>@<OPERATOR_TRUNK>` using customer number as caller ID (fallback to `OPERATOR_CALLER_ID`). Mark result `connected_to_operator` when operator answers.
6. If caller asks “شماره منو از کجا آوردید”: play `number`, then record one more reply; **yes** → play `yes` then operator flow, **no/unknown** → play `goodby`.
7. When any leg hangs up, remaining legs are torn down; results are reported to panel (if configured) via `report_result`.

## Result statuses (current)
- `connected_to_operator`: caller said yes and operator leg answered.
- `not_interested`: caller said no.
- `disconnected`: caller said yes but hung up before operator answered.
- `hangup`: caller hung up before a usable response.
- `missed`: no answer/busy/unreachable (or timeout watchdog).
- `failed:<reason>`: channel/operator failure (e.g., Busy/Failed/operator_failed).

## Extending
- Add new scenarios under `logic/` and wire them into `main.py` and `SessionManager`.
- Extend the YES-path by adding prompts and routing inside `logic/marketing_outreach.py`.
- When the panel API is ready, replace `_report_result` with a real client and update this README.
- Keep `.env.example` and `agent.md` in sync with any configuration or structural changes.

## Troubleshooting
- Confirm ARI credentials and app name; WebSocket URL must be reachable from the app host.
- Ensure custom sound files exist and are readable by Asterisk.
- If Vira tokens are missing, STT will return empty text and the call will follow the no-response path.
- Check logs for originate or playback errors; increase `LOG_LEVEL=DEBUG` for more detail. Logs go to stdout (journal in systemd) and `logs/app.log` with rotation; negative responses are duplicated in `logs/negative_stt.log`. Verify semaphore limits (`MAX_PARALLEL_*`) are high enough for expected load and that HTTP limits/timeouts are tuned for your network.
- Enhanced STT audio copies live under `/var/spool/asterisk/recording/enhanced/` for review; originals remain under `/var/spool/asterisk/recording/`.
- Panel disabled? leave `PANEL_BASE_URL`/`PANEL_API_TOKEN` empty and use `STATIC_CONTACTS`.
