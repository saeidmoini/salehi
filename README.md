# Salehi CallCenter - ARI Call Engine

Outbound/inbound ARI-driven call-control engine with YAML-based scenario flows and panel-driven dialing data. The app originates calls via Asterisk ARI, plays prompts, captures intent via STT+LLM, and enforces concurrency/rate limits.

## Features
- **Scenario-based architecture**: Scenarios are loaded from `config/scenarios/*.yaml` and filtered by `COMPANY`. Each scenario defines prompts, STT/LLM settings, and outbound/inbound flow steps.
- Bridge-centric ARI control (Asterisk 20 / FreePBX 17).
- Outbound dialer with per-line limits (concurrent/per-minute/per-day) and least-load line selection. Active lines come from panel `next-batch.outbound_lines`; env `OUTBOUND_NUMBERS` is used for startup line registration/default bootstrap.
- **Salehi scenario**: Language academy marketing with course-specific vocabulary (hello → alo → record → classify yes/no/number_question; yes plays `yes` then disconnects successfully; no/unknown plays `goodby`; number_question plays `number` then one more capture). Result reported as CONNECTED when user says yes.
- **Sina scenario**: General marketing with operator transfer (hello → alo → record → classify yes/no/number_question; yes plays `yes` + `onhold` then bridges operator; no/unknown plays `goodby`; number_question not used). Result reported as CONNECTED when operator answers.
- Inbound calls follow the same flow and are reported to the panel by phone when `number_id` is absent.
- Operator leg presents the customer's number as caller ID (fallback to `OPERATOR_CALLER_ID`) - Sina only.
- STT via Vira with ffmpeg pre-processing (denoise/normalize). Enhanced copies are saved under `/var/spool/asterisk/recording/enhanced/` for review. Positive/negative transcripts are logged (`logs/positive_stt.log`, `logs/negative_stt.log`). Empty/very short audio (<0.1s, RMS <0.001, or bytes <800) is treated as caller hangup and skipped.
- Optional GapGPT (gpt-4o-mini) for intent classification with scenario-specific guided examples (Salehi uses course/language names; Sina uses general responses).
- In-memory session manager ready for future Redis-backed storage.
- Async/await architecture (httpx + websockets) with semaphore-guarded STT/TTS/LLM calls and HTTP connection pooling. Origination throttle: 3 calls/sec; optional global inbound/outbound caps; per-line concurrency (`MAX_CONCURRENT_CALLS`) is shared across inbound+outbound on each line with inbound priority (outbound pauses while inbound is waiting). Vira STT quota (403) and LLM quota errors mark failures that pause the dialer and notify panel/SMS once thresholds are hit.

## Quick Start
1. Install Python 3.12.
2. Install system dependency `ffmpeg` (for prompt conversion): e.g. `sudo apt-get update && sudo apt-get install -y ffmpeg`.
3. Create a venv: `python -m venv venv && source venv/bin/activate`
4. Install deps: `pip install -r requirements.txt`
5. Copy `.env.example` to `.env` and fill in ARI, trunk, tokens, and either panel creds or `STATIC_CONTACTS` for local testing (set `PANEL_*` empty to disable panel).
6. Set company/scenario source: configure `COMPANY` and `SCENARIOS_DIR` in `.env`.
7. Ensure ARI dialplan sends calls to `Stasis(salehi)` and ARI user is configured.
8. Prompts live in-repo under `assets/audio/<scenario>/src/` (mp3 sources). To install them on Asterisk run (with the right permissions) `bash scripts/sync_audio.sh` which converts mp3→wav and copies to `/var/lib/asterisk/sounds/custom/` as `hello`, `alo`, `goodby`, `yes`, `number` (Salehi), `onhold` (Sina) - override target with `AST_SOUND_DIR`.
9. Run: `python main.py` (async entrypoint; startup auto-converts mp3→wav and syncs prompts to Asterisk).

**Note**: Ensure `AST_SOUND_DIR` points to your actual Asterisk custom sounds path (e.g., `/var/lib/asterisk/sounds/custom` or `/var/lib/asterisk/sounds/en/custom`). The app will try to sync to both base and `en/custom` when possible. If permissions block copying, run as a user with rights or pre-create the directories.

## Configuration
Set via environment or `.env`:
- **Company/scenarios**: `COMPANY` and `SCENARIOS_DIR`. The app loads all YAML scenarios from `SCENARIOS_DIR` and keeps only ones matching `scenario.company == COMPANY` (or empty company in YAML).
- ARI: `ARI_BASE_URL`, `ARI_WS_URL`, `ARI_APP_NAME`, `ARI_USERNAME`, `ARI_PASSWORD`
- Dialer/lines: `OUTBOUND_TRUNK`, `OUTBOUND_NUMBERS` (startup registration/default bootstrap), `DEFAULT_CALLER_ID`, `ORIGINATION_TIMEOUT`, `MAX_CONCURRENT_CALLS` (per-line total inbound+outbound), `MAX_CALLS_PER_MINUTE`, `MAX_CALLS_PER_DAY`, `MAX_ORIGINATIONS_PER_SECOND`, `DIALER_BATCH_SIZE`, `DIALER_DEFAULT_RETRY`
- Contacts: `STATIC_CONTACTS` (comma-separated) when panel is disabled
- Panel: `PANEL_BASE_URL`, `PANEL_API_TOKEN` (leave empty to disable panel). Panel `call_allowed=false` pauses new outbound; existing calls finish. Inbound results are reported by phone when `number_id` is missing.
- LLM: `GAPGPT_BASE_URL`, `GAPGPT_API_KEY` (optional; uses gpt-4o-mini). If LLM quota exceeded (403 error), dialer pauses and SMS/panel alerts are sent.
- Vira: `VIRA_STT_TOKEN`, `VIRA_TTS_TOKEN`, `VIRA_STT_URL`, `VIRA_TTS_URL`. If STT quota exceeded (403 error), dialer pauses and SMS/panel alerts are sent.
- Operator bridge (Sina only): `OPERATOR_EXTENSION`, `OPERATOR_TRUNK`, `OPERATOR_CALLER_ID`, `OPERATOR_TIMEOUT`
- Concurrency/timeouts: `HTTP_MAX_CONNECTIONS`, `HTTP_TIMEOUT`, `ARI_TIMEOUT`, `STT_TIMEOUT`, `TTS_TIMEOUT`, `LLM_TIMEOUT`, `MAX_PARALLEL_STT`, `MAX_PARALLEL_TTS`, `MAX_PARALLEL_LLM`
- Global caps (optional; 0 disables): `MAX_CONCURRENT_OUTBOUND_CALLS`, `MAX_CONCURRENT_INBOUND_CALLS`. Per-line caps: `MAX_CONCURRENT_CALLS` (shared inbound+outbound per line), `MAX_CALLS_PER_MINUTE`, `MAX_CALLS_PER_DAY`. Origination throttle: configurable via `MAX_ORIGINATIONS_PER_SECOND`.
- SMS alerts: `SMS_API_KEY`, `SMS_FROM`, `SMS_ADMINS`, `FAIL_ALERT_THRESHOLD` (pauses dialer and notifies after consecutive failures)
- Logging: `LOG_LEVEL`

## Panel API Contracts
- `GET /api/dialer/next-batch?company=<slug>&size=<1..500>`:
  - app reads `active_scenarios` as objects (`[{id,name,display_name}]`) and enables scenarios by `name`
  - app reads `outbound_lines` as objects (`[{id,phone_number,display_name}]`) and uses them as active dial lines
- `POST /api/dialer/register-scenarios`:
  - app sends `{ company, scenarios: [{name, display_name}] }` at startup
- `POST /api/dialer/register-outbound-lines`:
  - app sends `{ company, lines: [{phone_number, display_name}] }` at startup (from env `OUTBOUND_NUMBERS`)
- `POST /api/dialer/report-result`:
  - app sends id-based payload fields: `scenario_id`, `outbound_line_id`, plus `number_id`, `phone_number`, `company`, `status`, `attempted_at`, `reason`, optional `agent_id`, `agent_phone`, `user_message`, `call_allowed`
  - `batch_id` is not sent

## Architecture
- `main.py`: async entrypoint wiring settings, async ARI HTTP/WebSocket clients, scenario registry, flow engine, and dialer; runs under `asyncio.run`.
- `core/`: async ARI REST client (`ari_client.py`, httpx with pooling/timeouts) and WebSocket listener (`ari_ws.py`, websockets) that fans events into tasks.
- `sessions/`: async `SessionManager` (asyncio locks) that routes ARI events to scenario hooks and manages bridges.
- `logic/`: `dialer.py` for rate-limited origination (async loop), `flow_engine.py` for YAML-driven scenario execution, `scenario_registry.py` for loading/scoping scenarios, `base.py` for shared hooks.
- `integrations/panel/`: async client for panel dialer API (`next-batch`, `register-scenarios`, `register-outbound-lines`, `report-result`).
- `llm/`: async GapGPT wrapper (`client.py`) with semaphore limits.
- `stt_tts/`: async Vira STT/TTS wrappers with semaphore limits.
- `config/`: env loader and strongly-typed settings, including concurrency/timeouts.

## Scenario Flows

### Salehi Scenario (Language Academy Marketing)
1. Dialer pulls numbers from panel batches when allowed (or `STATIC_CONTACTS` fallback when panel disabled) and originates via `PJSIP/<dialstring>@<OUTBOUND_TRUNK>` where dialstring = last 4 digits of the chosen line + customer digits; per-line limits and least-load selection apply.
2. On answer, play `hello` greeting.
3. Play `alo` acknowledgment.
4. Record customer reply (10s max, 2s silence stop). If audio is empty/too-short, mark hangup; otherwise transcribe with Vira STT (audio enhanced via ffmpeg), and classify intent via LLM using course/language-specific examples (yes/no/number_question).
5. If intent is **yes**: play `yes` prompt, mark result as `connected_to_operator` (success), then disconnect. **No operator transfer occurs** - this is the successful outcome for Salehi.
6. If intent is **no** or **unknown**: play `goodby`, then hang up (negative/unknown transcripts logged to `logs/negative_stt.log` and `logs/unknown_stt.log`).
7. If caller asks "شماره منو از کجا آوردید" (number_question): play `number` response, then record one more reply; **yes** → play `yes` then disconnect as success, **no/unknown** → play `goodby`.
8. When call ends, results are reported to panel (if configured) via `report_result`.

### Sina Scenario (General Marketing with Operator Transfer)
1. Dialer pulls numbers from panel batches when allowed (or `STATIC_CONTACTS` fallback when panel disabled) and originates via `PJSIP/<dialstring>@<OUTBOUND_TRUNK>` where dialstring = last 4 digits of the chosen line + customer digits; per-line limits and least-load selection apply.
2. On answer, play `hello` greeting.
3. Play `alo` acknowledgment.
4. Record customer reply (10s max, 2s silence stop). If audio is empty/too-short, mark hangup; otherwise transcribe with Vira STT (audio enhanced via ffmpeg), and classify intent via LLM using general response examples (yes/no).
5. If intent is **yes**: play `yes` prompt, then play `onhold` music while originating operator leg to `PJSIP/<OPERATOR_EXTENSION>@<OPERATOR_TRUNK>` using customer number as caller ID (fallback to `OPERATOR_CALLER_ID`). Mark result `connected_to_operator` when operator answers. If operator fails to answer or call drops, mark as `disconnected` or `failed:operator_failed`.
6. If intent is **no** or **unknown**: play `goodby`, then hang up (negative/unknown transcripts logged to `logs/negative_stt.log` and `logs/unknown_stt.log`).
7. When any leg hangs up, remaining legs are torn down; results are reported to panel (if configured) via `report_result`.

## Result Statuses and Panel Reporting

Internal results are mapped to standardized panel statuses when reporting. See [PANEL_STATUSES.md](PANEL_STATUSES.md) for comprehensive documentation.

### Key Result Statuses

**Success:**
- `connected_to_operator` → Panel status: **CONNECTED**
  - **Salehi**: User said yes, `yes` prompt played, call disconnected (no operator transfer)
  - **Sina**: User said yes, operator answered and was connected

**User Declined:**
- `not_interested` → Panel status: **NOT_INTERESTED**
  - User explicitly said no or expressed disinterest

**User Didn't Respond:**
- `hangup` → Panel status: **HANGUP**
  - User hung up before providing usable response
  - Empty/invalid audio (local check or Vira "Empty Audio file") is treated as hangup

**No Answer:**
- `missed` → Panel status: **MISSED**
  - No answer, busy signal, or timeout watchdog triggered

**Disconnect Before Operator (Sina only):**
- `disconnected` → Panel status: **DISCONNECTED**
  - User said yes but hung up before operator answered

**Technical Failures:**
- `failed:operator_failed` → Panel status: **FAILED** (Sina only)
  - Operator leg failed to connect
- `failed:stt_failure` → Panel status: **NOT_INTERESTED**
  - STT transcription failed (treated as non-response)
- `failed:vira_quota` → Panel status: **FAILED**
  - Vira STT quota exceeded (403 error); dialer pauses and SMS/panel alerts sent
- `failed:llm_quota` → Panel status: **FAILED**
  - LLM quota exceeded (403 error); dialer pauses and SMS/panel alerts sent

**Early Detection (SIP Cause Codes):**
- `busy` (SIP cause 17) → Panel status: **BUSY**
- `power_off` (SIP causes 18/19/20) → Panel status: **POWER_OFF**
- `banned` (SIP causes 21/34/41/42) → Panel status: **BANNED**

**Unknown/Unclear:**
- `unknown` → Panel status: **UNKNOWN**
  - Intent classification unclear or ambiguous response

## Deployment

### Automated Deployment Script
Use `update.sh` for deployment:

```bash
./update.sh
```

**What it does:**
1. Pulls latest code from `main` branch
2. Uses your `.env` values (including `COMPANY`, `SCENARIOS_DIR`, panel credentials, and line config)
3. Updates Python dependencies
4. Sets proper permissions for asterisk user
5. Restarts the configured systemd service for this deployment

## Extending
- Add new scenarios by creating YAML files under `config/scenarios/` with unique `scenario.name` (within a company) and matching `scenario.company`.
- Modify existing behavior by editing flow steps in scenario YAML files and, when needed, flow execution logic in `logic/flow_engine.py`.
- Add new audio prompts by placing MP3 files in `assets/audio/<scenario>/src/` and running `bash scripts/sync_audio.sh` to convert and deploy.
- Keep `.env.example` and documentation in sync with any configuration or structural changes.

## Troubleshooting

### Common Issues

**Wrong scenario running:**
- Check `COMPANY` and `SCENARIOS_DIR` in `.env`
- Verify scenario YAML has matching `scenario.company`
- Confirm panel `active_scenarios[].name` matches scenario `name`

**Deployment script errors:**
- "Permission denied" on audio sync: Run `update.sh` as user with sudo access or manually set permissions on `/var/lib/asterisk/sounds/custom/`
- Service not restarting: verify your service unit points to this app directory and python environment.

**Call flow issues:**
- Confirm ARI credentials and app name; WebSocket URL must be reachable from the app host.
- Ensure custom sound files exist and are readable by Asterisk at `/var/lib/asterisk/sounds/custom/` (should contain `hello.wav`, `alo.wav`, `goodby.wav`, `yes.wav`, and for Salehi: `number.wav`, for Sina: `onhold.wav`).
- If Vira tokens are missing, STT will return empty text and the call will follow the no-response path.

**Quota errors (Vira STT or GapGPT LLM):**
- If you see "failed:vira_quota" or "failed:llm_quota" in logs, the service has detected a 403 error indicating quota exhaustion
- Dialer automatically pauses and SMS/panel alerts are sent to admins
- Check your Vira/GapGPT account balance and increase quota
- Dialer will auto-resume on next successful API call after quota is restored

**Logging:**
- Check logs for originate or playback errors; increase `LOG_LEVEL=DEBUG` for more detail.
- Logs go to stdout (journal in systemd) and `logs/app.log` with rotation
- Transcripts logged separately: `logs/positive_stt.log` (YES), `logs/negative_stt.log` (NO), `logs/unknown_stt.log` (UNKNOWN)
- Verify semaphore limits (`MAX_PARALLEL_*`) are high enough for expected load and that HTTP limits/timeouts are tuned for your network.
- Enhanced STT audio copies live under `/var/spool/asterisk/recording/enhanced/` for review; originals remain under `/var/spool/asterisk/recording/`.

**Testing without panel:**
- Leave `PANEL_BASE_URL`/`PANEL_API_TOKEN` empty in `.env`
- Use `STATIC_CONTACTS=09123456789,09987654321` for test numbers
- Results will only be logged locally, not reported to panel
