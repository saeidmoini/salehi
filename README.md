# Salehi ARI Call Engine

Outbound, ARI-driven call-control engine for a language academy marketing campaign. The app originates calls from a configured trunk, plays pre-recorded prompts, captures interest via STT, and enforces concurrency/rate limits.

## Features
- Bridge-centric ARI control (Asterisk 20 / FreePBX 17).
- Outbound dialer with limits: concurrent calls, per-minute, per-day, and call windows.
- Scenario logic for marketing outreach (hello prompt → yes/no → second prompt → yes/no → optional operator bridge for outbound only; inbound calls do not transfer).
- STT/TTS hooks via Vira with separate STT/TTS tokens; optional GapGPT fallback for intent classification.
- In-memory session manager ready for future Redis-backed storage.
- Async/await architecture (httpx + websockets) with semaphore-guarded STT/TTS/LLM calls and HTTP connection pooling.

## Quick Start
1. Install Python 3.12.
2. Install system dependency `ffmpeg` (for prompt conversion): e.g. `sudo apt-get update && sudo apt-get install -y ffmpeg`.
2. Create a venv: `python -m venv venv && source venv/bin/activate`
3. Install deps: `pip install -r requirements.txt`
4. Copy `.env.example` to `.env` and fill in ARI, trunk, and token details.
5. Ensure ARI dialplan sends calls to `Stasis(salehi)` and ARI user is configured.
6. Prompts live in-repo under `assets/audio/` (mp3 sources and 16 kHz mono wav). To install them on Asterisk run (with the right permissions) `bash scripts/sync_audio.sh` which copies wavs to `/var/lib/asterisk/sounds/custom/` as `hello`, `goodby`, `second` (override target with `AST_SOUND_DIR`).
7. Run: `python main.py` (async entrypoint; startup auto-converts mp3→wav and syncs prompts to Asterisk).

Note: Ensure `AST_SOUND_DIR` points to your actual Asterisk custom sounds path (e.g., `/var/lib/asterisk/sounds/custom` or `/var/lib/asterisk/sounds/en/custom`). The app will try to sync to both base and `en/custom` when possible. If permissions block copying, run as a user with rights or pre-create the directories.

## Configuration
Set via environment or `.env`:
- ARI: `ARI_BASE_URL`, `ARI_WS_URL`, `ARI_APP_NAME`, `ARI_USERNAME`, `ARI_PASSWORD`
- Dialer: `OUTBOUND_TRUNK`, `DEFAULT_CALLER_ID`, `ORIGINATION_TIMEOUT`, `MAX_CONCURRENT_CALLS`, `MAX_CALLS_PER_MINUTE`, `MAX_CALLS_PER_DAY`, `CALL_WINDOW_START`, `CALL_WINDOW_END`
- Contacts: `STATIC_CONTACTS` (comma-separated; defaults to `+989000000000` until the external API is ready)
- LLM: `GAPGPT_BASE_URL`, `GAPGPT_API_KEY` (optional)
- Vira: `VIRA_STT_TOKEN`, `VIRA_TTS_TOKEN`, `VIRA_STT_URL`, `VIRA_TTS_URL` (`VIRA_TOKEN` is unused for STT)
- Operator bridge: `OPERATOR_EXTENSION`, `OPERATOR_TRUNK`, `OPERATOR_CALLER_ID`, `OPERATOR_TIMEOUT`
- Concurrency/timeouts: `HTTP_MAX_CONNECTIONS`, `HTTP_TIMEOUT`, `ARI_TIMEOUT`, `STT_TIMEOUT`, `TTS_TIMEOUT`, `LLM_TIMEOUT`, `MAX_PARALLEL_STT`, `MAX_PARALLEL_TTS`, `MAX_PARALLEL_LLM`
- Logging: `LOG_LEVEL`

## Architecture
- `main.py`: async entrypoint wiring settings, async ARI HTTP/WebSocket clients, session manager, dialer, and marketing scenario; runs under `asyncio.run`.
- `core/`: async ARI REST client (`ari_client.py`, httpx with pooling/timeouts) and WebSocket listener (`ari_ws.py`, websockets) that fans events into tasks.
- `sessions/`: async `SessionManager` (asyncio locks) that routes ARI events to scenario hooks and manages bridges.
- `logic/`: `dialer.py` for rate-limited origination (async loop); `marketing_outreach.py` for scenario logic; `base.py` for shared scenario hooks.
- `llm/`: async GapGPT wrapper (`client.py`) with semaphore limits.
- `stt_tts/`: async Vira STT/TTS wrappers with semaphore limits.
- `config/`: env loader and strongly-typed settings, including concurrency/timeouts.

## Scenario Flow (current)
1. Dialer pulls numbers from `STATIC_CONTACTS` and originates via `PJSIP/<number>@<OUTBOUND_TRUNK>` respecting all limits and call windows. Inbound calls follow the same prompts but skip operator transfer.
2. On answer, play `hello`.
3. Record a short reply, transcribe with Vira STT (hotwords tuned to yes/no lists), and classify intent (heuristic with GapGPT fallback if available).
4. If intent is **no** or silence/unknown: play `goodby`, then hang up.
5. If intent is **yes**: play `yes`, then originate/bridge operator leg to `PJSIP/<OPERATOR_EXTENSION>@<OPERATOR_TRUNK>` (default 200). Mark result `connected_to_operator`. Inbound calls skip operator transfer.
6. If caller asks “شماره منو از کجا آوردید”: play `number`, then play `goodby` and hang up.
7. When any leg hangs up, remaining legs are torn down; results are logged in `_report_result` for the future panel API.

## Result statuses (current)
- `connected_to_operator`: caller said yes twice and was bridged to the operator leg (operator leg answered).
- `not_interested`: caller said no at any decision point.
- `user_didnt_answer`: no usable speech/intent detected.
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
- Check logs for originate or playback errors; increase `LOG_LEVEL=DEBUG` for more detail. Logs go to stdout (journal in systemd) and `logs/app.log` with rotation. Verify semaphore limits (`MAX_PARALLEL_*`) are high enough for expected load and that HTTP limits/timeouts are tuned for your network.
