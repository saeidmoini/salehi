# Agent Notes

This repository hosts an ARI-based call-control engine for outbound/inbound marketing calls. The core rules for the project live in `prompt.txt`; always read and obey it before making changes.

## Layout & Responsibilities
- `main.py`: async entrypoint; wires config, ARI clients, WebSocket listener, dialer, and current scenario.
- `config/`: environment loader (`get_settings`) and dataclasses for ARI, GapGPT, Vira, dialer limits, concurrency, and timeouts.
- `core/`: async ARI HTTP client (`ari_client.py`, httpx) and WebSocket listener (`ari_ws.py`, websockets).
- `sessions/`: in-memory session/bridge/leg models and async `SessionManager` for routing ARI events to scenario hooks.
- `logic/`: YAML-driven flow engine (`flow_engine.py`) and scenario registry (`scenario_registry.py`) plus dialer/rate-limit logic (`dialer.py`). Active lines come from panel `next-batch.outbound_lines`; env `OUTBOUND_NUMBERS` is used for startup outbound-line registration/bootstrap.
- `llm/`: async GapGPT wrapper with semaphore.
- `stt_tts/`: async Vira STT/TTS wrappers with semaphore guards; STT audio is preprocessed via ffmpeg (denoise/normalize) and enhanced copies are saved to `/var/spool/asterisk/recording/enhanced/` for review. Empty/too-short audio (<0.1s, RMS<0.001, or bytes<800) is treated as caller hangup; Vira “Empty Audio file” also maps to hangup.
- `integrations/panel/`: async client for panel dialer API (`next-batch`, `register-scenarios`, `register-outbound-lines`, `report-result`).

- `.env.example`: keep this updated; never commit real credentials/tokens.
- `.env`: ignored by git; may contain real ARI, Vira, and GapGPT tokens.
- `assets/audio/`: source mp3s live in `assets/audio/src`, converted 16 kHz mono wavs in `assets/audio/wav`. Use `scripts/sync_audio.sh` to copy wavs into `/var/lib/asterisk/sounds/custom/` as `hello`, `goodby`, `yes`, `number`, `onhold` (override target with `AST_SOUND_DIR`).

## Working Rules
- Never hard-code credentials; read from environment or `.env` (loaded by `config/settings.py`). Keep `.env` out of git.
- If you change architecture or add modules, update both `agent.md` and `README.md`.
- Follow bridge-centric design: every session should have a mixing bridge managed by ARI.
- Keep code modular; avoid globals; prefer classes in the existing packages.
- When adding scenarios, create a new module under `logic/` and wire it in `main.py` and `SessionManager` hooks. Preserve the existing marketing scenario unless the user replaces it.
- Rate limiting is handled by `logic/dialer.py` (per-line concurrency via `MAX_CONCURRENT_CALLS` shared across inbound+outbound on the same line, inbound waits have priority and block outbound on that line, per-minute, per-day, and `MAX_ORIGINATIONS_PER_SECOND`) plus optional global caps `MAX_CONCURRENT_OUTBOUND_CALLS` / `MAX_CONCURRENT_INBOUND_CALLS` (0 disables). Panel `call_allowed` gates outbound; `STATIC_CONTACTS` is used when panel is disabled. Vira balance errors and LLM quota errors mark failures so the dialer pauses and notifies panel/SMS once the failure threshold is reached.
- Current panel payload conventions:
  - `register-scenarios`: `{company, scenarios:[{name, display_name}]}`
  - `register-outbound-lines`: `{company, lines:[{phone_number, display_name}]}`
  - `next-batch.active_scenarios`: list of objects with `id` and `name`
  - `report-result`: send `scenario_id` and `outbound_line_id` (not `batch_id`)
- STT/TTS hooks use Vira endpoints; tokens are separate for STT and TTS (`VIRA_STT_TOKEN`, `VIRA_TTS_TOKEN`). Audio is enhanced before STT; originals remain under `/var/spool/asterisk/recording/`, enhanced copies in `/var/spool/asterisk/recording/enhanced/`.
- Recording/transcription fetches stored recordings via the async `AriClient`; transcription runs as async tasks behind Vira STT semaphore limits; intent is LLM-only (examples provided). Positive/negative transcripts are logged (`logs/positive_stt.log`, `logs/negative_stt.log`).
- Logging uses the standard library. Negative transcripts go to `logs/negative_stt.log`; positive (yes) transcripts go to `logs/positive_stt.log`.
- Audio sync is automatic at startup: mp3s under `assets/audio/src` are converted to wav (16k mono) and copied to the configured `AST_SOUND_DIR` for playback as `sound:custom/<name>`.
- Everything is async/await: no blocking `time.sleep`. HTTP uses httpx.AsyncClient with connection pooling limits; WebSocket uses `websockets`. STT uses `requests` inside `asyncio.to_thread` for compatibility. Protect session dictionaries with `asyncio.Lock`, and guard STT/TTS/LLM with semaphores (`MAX_PARALLEL_*`).

## Commit/Change Guidance
- Use conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`).
- Before adding dependencies, ensure the user has approved them.
- When extending call behavior, edit scenario YAML flow steps under `config/scenarios/` and document required audio assets (hello/goodby/yes/onhold/number). Audio must exist on the Asterisk host—use `scripts/sync_audio.sh` to copy from `assets/audio/wav` into `sounds/custom/`.
- If you add APIs for reporting results, encapsulate them in a dedicated client/module and keep network details configurable.

## Running & Testing
- Create/activate the venv, install `requirements.txt`, and run `python main.py` (asyncio entrypoint).
- Ensure Asterisk ARI is reachable at the configured URLs; confirm custom prompt audio files exist under `sounds/custom/` on the Asterisk box.
- Manual STT/LLM tokens are optional; without them, the scenario will still run but will classify interest heuristically and may hang up after the goodbye prompt.
- The operator transfer uses `OPERATOR_EXTENSION`/`OPERATOR_TRUNK`; caller ID sent to the operator leg is the customer number (fallback `OPERATOR_CALLER_ID`). That leg is added to the existing mixing bridge; result is marked `connected_to_operator` when the operator answers. Inbound calls follow the same flow and are reported to panel by phone when `number_id` is missing.
- Recording: max 10s, silence cutoff 2s; empty/short audio is skipped before STT.
