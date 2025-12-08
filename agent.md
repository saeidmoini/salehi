# Agent Notes

This repository hosts an ARI-based call-control engine for outbound marketing calls. The core rules for the project live in `prompt.txt`; always read and obey it before making changes.

## Layout & Responsibilities
- `main.py`: async entrypoint; wires config, ARI clients, WebSocket listener, dialer, and current scenario.
- `config/`: environment loader (`get_settings`) and dataclasses for ARI, GapGPT, Vira, dialer limits, concurrency, and timeouts.
- `core/`: async ARI HTTP client (`ari_client.py`, httpx) and WebSocket listener (`ari_ws.py`, websockets).
- `sessions/`: in-memory session/bridge/leg models and async `SessionManager` for routing ARI events to scenario hooks.
- `logic/`: scenario modules. Current scenario: `marketing_outreach.py` (single capture: hello → record → classify yes/no/number-question; yes plays `yes` then operator bridge; no/unknown plays `goodby`; number-question plays `number` then records once more and routes yes/ no). Dialer/rate-limit logic in `logic/dialer.py` (pulls batches from panel when configured).
- `llm/`: async GapGPT wrapper with semaphore.
- `stt_tts/`: async Vira STT/TTS wrappers with semaphore guards.
- `integrations/panel/`: async client for panel dialer API (next-batch/report-result).

- `.env.example`: keep this updated; never commit real credentials/tokens.
- `.env`: ignored by git; may contain real ARI, Vira, and GapGPT tokens.
- `assets/audio/`: source mp3s live in `assets/audio/src`, converted 16 kHz mono wavs in `assets/audio/wav`. Use `scripts/sync_audio.sh` to copy wavs into `/var/lib/asterisk/sounds/custom/` as `hello`, `goodby`, `second` (override target with `AST_SOUND_DIR`).

## Working Rules
- Never hard-code credentials; read from environment or `.env` (loaded by `config/settings.py`). Keep `.env` out of git.
- If you change architecture or add modules, update both `agent.md` and `README.md`.
- Follow bridge-centric design: every session should have a mixing bridge managed by ARI.
- Keep code modular; avoid globals; prefer classes in the existing packages.
- When adding scenarios, create a new module under `logic/` and wire it in `main.py` and `SessionManager` hooks. Preserve the existing marketing scenario unless the user replaces it.
- Rate limiting is handled by `logic/dialer.py` (concurrency, per-minute, per-day, call windows). Adjust via env vars and document changes.
- STT/TTS hooks use Vira endpoints; tokens are separate for STT and TTS (`VIRA_STT_TOKEN`, `VIRA_TTS_TOKEN`). `VIRA_TOKEN` is unused for STT.
- Recording/transcription fetches stored recordings via the async `AriClient`; transcription runs as async tasks behind Vira STT semaphore limits with hotwords seeded from scenario tokens (single words).
- Logging uses the standard library. Keep logs informative for Stasis events, playbacks, originates, STT/LLM failures.
- Audio sync is automatic at startup: mp3s under `assets/audio/src` are converted to wav (16k mono) and copied to the configured `AST_SOUND_DIR` for playback as `sound:custom/<name>`.
- Everything is async/await: no blocking `time.sleep`. HTTP uses httpx.AsyncClient with connection pooling limits; WebSocket uses `websockets`. STT uses `requests` inside `asyncio.to_thread` for compatibility. Protect session dictionaries with `asyncio.Lock`, and guard STT/TTS/LLM with semaphores (`MAX_PARALLEL_*`).

## Commit/Change Guidance
- Use conventional commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`, `test:`).
- Before adding dependencies, ensure the user has approved them.
- When extending the YES-path questionnaire, add prompts and flows in `logic/marketing_outreach.py` and document required audio assets (hello/goodby/second). Audio must exist on the Asterisk host—use `scripts/sync_audio.sh` to copy from `assets/audio/wav` into `sounds/custom/`.
- If you add APIs for reporting results, encapsulate them in a dedicated client/module and keep network details configurable.

## Running & Testing
- Create/activate the venv, install `requirements.txt`, and run `python main.py` (asyncio entrypoint).
- Ensure Asterisk ARI is reachable at the configured URLs; confirm custom prompt audio files exist under `sounds/custom/` on the Asterisk box.
- Manual STT/LLM tokens are optional; without them, the scenario will still run but will classify interest heuristically and may hang up after the goodbye prompt.
- The operator transfer uses `OPERATOR_EXTENSION`/`OPERATOR_TRUNK` env vars and originates a new leg with appArgs `operator,<session_id>,<endpoint>`. That leg is added to the existing mixing bridge; result is marked `connected_to_operator` when the operator answers. Operator originate is guarded to run once per session and is skipped for inbound-only calls.
- Recording: max 10s, silence cutoff 2s. The processing beep before STT is removed.
