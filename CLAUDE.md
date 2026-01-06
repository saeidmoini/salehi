# CLAUDE.md - Salehi CallCenter Project

## Project Overview

**Salehi** is an advanced ARI-based (Asterisk REST Interface) call control engine designed for outbound and inbound marketing campaigns for a language academy. The system originates calls, plays prompts, captures customer responses via Speech-to-Text (STT), classifies intent using an LLM, and manages call routing with sophisticated concurrency and rate limiting.

**Technology Stack**: Python 3.12, Asyncio, Asterisk 20/FreePBX 17, httpx, websockets

**Scenario Support**: The project supports multiple call flow scenarios (Salehi and Agrad) through a single codebase. Switch scenarios using the `SCENARIO` environment variable. See [Scenario Management Guide](docs/scenario_management.md) for details.

---

## Project Architecture

### High-Level System Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        main.py (Entrypoint)                  │
│  - Initializes all clients and managers                     │
│  - Sets up async event loop                                 │
│  - Manages graceful shutdown                                │
└──────────────┬──────────────────────────────────────────────┘
               │
    ┌──────────┴──────────┬─────────────┬──────────────┐
    │                     │             │              │
    ▼                     ▼             ▼              ▼
┌─────────┐        ┌──────────┐   ┌─────────┐   ┌──────────┐
│ARI WS   │◄──────►│ Session  │◄──┤ Dialer  │   │Marketing │
│Listener │        │ Manager  │   │ Engine  │   │Scenario  │
└─────────┘        └────┬─────┘   └─────────┘   └──────────┘
                        │
              ┌─────────┼─────────┐
              ▼         ▼         ▼
         ┌────────┐ ┌──────┐ ┌──────────┐
         │ARI HTTP│ │ STT  │ │   LLM    │
         │ Client │ │Client│ │  Client  │
         └────────┘ └──────┘ └──────────┘
              │         │         │
              ▼         ▼         ▼
         ┌────────┐ ┌──────┐ ┌──────────┐
         │Asterisk│ │ Vira │ │  GapGPT  │
         │  ARI   │ │ API  │ │   API    │
         └────────┘ └──────┘ └──────────┘
```

### Directory Structure

```
/media/saeid/Software/Projects/Salehi/
├── main.py                    # Async entrypoint and application wiring
├── config/                    # Environment settings and configuration
│   └── settings.py           # Strongly-typed settings dataclasses
├── core/                      # ARI communication layer
│   ├── ari_client.py         # HTTP REST client for ARI (httpx)
│   └── ari_ws.py             # WebSocket event listener
├── sessions/                  # Session state management
│   ├── session.py            # Session/bridge/leg data models
│   └── session_manager.py    # Event routing and lifecycle management
├── logic/                     # Business logic and scenarios
│   ├── base.py               # Base scenario handler interface
│   ├── dialer.py             # Outbound dialer with rate limiting
│   └── marketing_outreach.py # Marketing scenario implementation
├── integrations/              # External service integrations
│   ├── panel/                # Panel dialer API client
│   │   └── client.py
│   └── sms/                  # SMS alerting (Melipayamak)
│       └── melipayamak.py
├── llm/                       # LLM integration
│   └── client.py             # GapGPT wrapper (OpenAI-compatible)
├── stt_tts/                   # Speech services
│   ├── vira_stt.py           # Vira STT client with audio enhancement
│   └── vira_tts.py           # Vira TTS client
├── utils/                     # Utilities
│   └── audio_sync.py         # Audio conversion and deployment
├── assets/                    # Audio prompt files
│   └── audio/
│       ├── src/              # Source MP3 files
│       └── wav/              # Converted WAV/ULAW/ALAW files
├── scripts/                   # Deployment scripts
│   └── sync_audio.sh
├── docs/                      # Documentation
│   ├── branching.md
│   └── llm_vira_usage.md
├── logs/                      # Application logs
├── requirements.txt           # Python dependencies
├── .env.example              # Environment configuration template
├── update.sh                 # Deployment script
└── README.md                 # Main documentation
```

---

## Core Components

### 1. Main Application ([main.py](main.py))

**Purpose**: Application entrypoint that orchestrates all components

**Responsibilities**:
- Configure logging with rotation (console + file at `logs/app.log`)
- Initialize async clients (ARI, STT, TTS, LLM, Panel)
- Create semaphores for concurrency control
- Wire SessionManager, Dialer, and MarketingScenario together
- Auto-convert and sync audio assets on startup
- Handle graceful shutdown on SIGINT/SIGTERM

**Key Flow**:
```python
async def main():
    # 1. Setup logging
    # 2. Load settings from .env
    # 3. Initialize all async clients
    # 4. Create concurrency semaphores
    # 5. Initialize SessionManager → Dialer → MarketingScenario
    # 6. Start ARI WebSocket listener
    # 7. Start dialer loop
    # 8. Wait for shutdown signal
    # 9. Cleanup resources
```

---

### 2. Configuration Layer ([config/settings.py](config/settings.py))

**Purpose**: Centralized configuration management with strong typing

**Key Features**:
- Custom `.env` file loader (no external dependencies)
- Strongly-typed dataclasses for all settings
- Validation and defaults
- Environment variable override support

**Configuration Categories**:
- `AriSettings`: ARI connection details (HTTP, WebSocket, app name, credentials)
- `GapGPTSettings`: LLM configuration (API key, base URL, model)
- `ViraSettings`: STT/TTS credentials and endpoints
- `DialerSettings`: Rate limits, batch size, retry intervals
- `OperatorSettings`: Transfer target configuration
- `PanelSettings`: External panel API integration
- `AudioSettings`: Audio file paths and directories
- `ConcurrencySettings`: Semaphore limits for services
- `TimeoutSettings`: Service-specific timeout values
- `SMSSettings`: SMS alerting configuration

**Usage Example**:
```python
from config.settings import Settings

settings = Settings()
ari_url = settings.ari.base_url
max_calls = settings.dialer.max_concurrent_calls
```

---

### 3. ARI Communication Layer

#### HTTP Client ([core/ari_client.py](core/ari_client.py))

**Purpose**: Async HTTP client for Asterisk REST Interface operations

**Key Operations**:
- **Bridge Management**: create, delete, add/remove channels
- **Channel Control**: answer, hangup, originate, mute/unmute
- **Media Playback**: play audio on channel/bridge
- **Recording**: start/stop channel/bridge recording
- **Channel Variables**: read SIP headers and custom variables
- **Connection Pooling**: httpx.AsyncClient with pooling

**Usage Example**:
```python
ari_client = AriClient(settings.ari)
await ari_client.answer(channel_id)
await ari_client.play_on_bridge(bridge_id, "sound:custom/hello")
```

#### WebSocket Listener ([core/ari_ws.py](core/ari_ws.py))

**Purpose**: WebSocket listener for real-time ARI events

**Key Features**:
- Auto-reconnection on connection loss
- Event fan-out as async tasks
- Graceful shutdown handling
- Ping/pong keepalive

**Events Handled**:
- `StasisStart`: New channel enters application
- `ChannelStateChange`: Channel state transitions
- `ChannelHangupRequest`: Hangup initiated
- `ChannelDestroyed`: Channel cleanup
- `PlaybackStarted/Finished`: Media playback events
- `RecordingFinished/Failed`: Recording completion
- `StasisEnd`: Channel exits application

---

### 4. Session Management

#### Data Models ([sessions/session.py](sessions/session.py))

**Purpose**: Data models representing call sessions and their state

**Key Models**:

```python
class LegDirection(Enum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    OPERATOR = "operator"

class LegState(Enum):
    CREATED = "created"
    RINGING = "ringing"
    ANSWERED = "answered"
    HUNGUP = "hungup"
    FAILED = "failed"

class CallLeg:
    channel_id: str
    direction: LegDirection
    state: LegState
    number: str
    caller_id: str
    # ... timestamps, metadata

class BridgeInfo:
    bridge_id: str
    created_at: float

class Session:
    session_id: str
    bridge: Optional[BridgeInfo]
    customer_leg: Optional[CallLeg]
    operator_leg: Optional[CallLeg]
    status: SessionStatus
    metadata: dict
    # ... async lock, playbacks, responses
```

#### Session Manager ([sessions/session_manager.py](sessions/session_manager.py:1-713))

**Purpose**: Central orchestrator for session lifecycle and event routing

**Key Responsibilities**:

1. **Session Lifecycle**:
   - Create sessions on StasisStart
   - Track all active sessions
   - Cleanup on session completion

2. **Event Routing**:
   - Maps ARI events to scenario hooks
   - Routes events to appropriate sessions
   - Handles channel state changes

3. **Bridge Management**:
   - Creates mixing bridge for every session
   - Adds channels to bridges
   - Cleans up bridges on completion

4. **Channel Mapping**:
   - Maintains channel_id → session mapping
   - Tracks playback_id → session mapping
   - Manages recording_id → session mapping

5. **Inbound Handling**:
   - Auto-answers inbound calls
   - Extracts caller info from SIP headers
   - Normalizes phone numbers

6. **Concurrency Control**:
   - Per-line capacity limits (shared inbound+outbound)
   - Inbound priority over outbound
   - Waiting queue for inbound calls when at capacity

7. **Cleanup**:
   - Proactive hangup of orphaned channels
   - Resource release on session end
   - Dedicated hangup logging

**Special Features**:
- **Hangup Logger**: Dedicated log file at `logs/hangups.log`
- **User Drop Timing**: Logs customer disconnection timing at `logs/userdrop.log`
- **SIP Header Extraction**: Reads Diversion, P-Asserted-Identity headers
- **Number Normalization**: Adds leading zero for 10-digit Iranian mobiles
- **Line Matching**: Maps inbound DIDs to configured outbound lines

**Key Methods**:
```python
async def create_session(direction, number, line) -> Session
async def handle_stasis_start(event)  # Entry point for new channels
async def handle_channel_hangup_request(event)
async def cleanup_session(session_id)
def has_line_capacity(line, direction) -> bool
async def enqueue_inbound(session)
```

---

### 5. Dialer Engine ([logic/dialer.py](logic/dialer.py:1-493))

**Purpose**: Outbound call origination with sophisticated multi-dimensional rate limiting

**Rate Limiting Architecture**:

**Per-Line Limits** (each outbound line independently):
- Max concurrent calls (shared inbound+outbound)
- Max calls per minute (sliding window)
- Max calls per day (resets at midnight)
- Max 1 origination per second (anti-spam)

**Global Limits** (across all lines):
- Max concurrent outbound calls (optional, 0=unlimited)
- Max concurrent inbound calls (optional, 0=unlimited)
- Max originations per second (optional, throttles all lines)

**Priority Mechanisms**:
- **Inbound Priority**: Outbound pauses when inbound calls are waiting
- **Operator Priority**: Outbound pauses while operator leg originates

**Key Features**:

1. **Least-Load Line Selection**:
   - Chooses line with fewest active calls
   - Respects per-line and global limits
   - Skips lines at capacity

2. **Panel Integration**:
   - Fetches batches of numbers to call
   - Reports results back to panel
   - Respects `call_allowed` flag
   - Updates active agent roster

3. **Static Contact List**:
   - Fallback when panel disabled
   - Configured via `STATIC_CONTACTS` env var

4. **Timeout Watchdog**:
   - Marks calls as "missed" if no events received within timeout
   - Prevents stuck sessions

5. **Failure Detection**:
   - Counts consecutive origination failures
   - Auto-pauses dialer on threshold
   - Sends SMS alerts to admins
   - Notifies panel of pause

6. **SMS Alerts**:
   - Consecutive failure threshold (default: 3)
   - Admin notification via Melipayamak
   - Includes failure reason

**Key Methods**:
```python
async def dialer_loop():
    # Main dialer loop
    while True:
        await fetch_batch()  # From panel or static list
        await originate_next_call()
        await asyncio.sleep(interval)

def select_least_loaded_line() -> str
async def check_origination_allowed(line) -> bool
async def originate_call(contact, line)
async def handle_consecutive_failures(reason)
```

**Panel Integration Flow**:
```python
# Fetch batch
batch = await panel_client.get_next_batch(
    size=DIALER_BATCH_SIZE,
    timezone="+0330",
    schedule_version="v1"
)

# Extract contacts and agents
contacts = batch["contacts"]
active_agents = batch.get("active_agents", [])

# After call completes
await panel_client.report_result(
    contact_id=contact_id,
    status=status,
    user_message=transcript,
    agent_id=agent_id
)
```

---

### 6. Marketing Scenario Logic ([logic/marketing_outreach.py](logic/marketing_outreach.py:1-1072))

**Purpose**: Implements the marketing campaign call flow with LLM-guided intent classification

**Call Flow**:

```
1. Call Answered
   ↓
2. Play "hello" prompt
   ↓
3. Record customer response (10s max, 2s silence cutoff)
   ↓
4. Audio Enhancement (ffmpeg noise reduction)
   ↓
5. Transcribe via Vira STT
   ↓
6. Classify Intent via LLM
   ↓
7. Intent Routing:
   ├─ YES → Play "yes" → Disconnect (Salehi branch)
   ├─ NO → Play "goodby" → Hangup
   ├─ NUMBER_QUESTION → Play "number" → Record again → Loop to step 4
   └─ UNKNOWN → Play "goodby" → Hangup
```

**Audio Enhancement Pipeline**:

The system preprocesses all recordings before STT to improve transcription accuracy:

```bash
ffmpeg -i input.wav \
  -af "highpass=f=120, lowpass=f=3800, afftdn=nf=-25, loudnorm" \
  -ar 16000 -ac 1 \
  enhanced.wav
```

**Filters Applied**:
- **Highpass (120 Hz)**: Remove low-frequency rumble
- **Lowpass (3800 Hz)**: Remove high-frequency noise
- **afftdn**: Adaptive FFT denoiser for background noise
- **loudnorm**: EBU R128 loudness normalization
- **Resample**: Convert to 16kHz mono

Enhanced audio is saved to: `/var/spool/asterisk/recording/enhanced/`

**Intent Classification**:

The LLM classifies customer responses into 4 categories:

```python
INTENTS = ["yes", "no", "number_question", "unknown"]

CLASSIFICATION_PROMPT = """
Based on this Persian phone call transcript, classify the customer's intent:

Transcript: "{transcript}"

Categories:
- yes: Customer is interested/agrees
- no: Customer refuses/not interested
- number_question: Asks where we got their number
- unknown: Unclear/irrelevant response

Respond with ONLY the category name.
"""
```

**LLM Fallback**: If LLM fails, uses token-based heuristics:
- Positive tokens: "بله", "آره", "باشه", "حتما", etc.
- Negative tokens: "نه", "خیر", "نمیخوام", etc.

**Hotwords for STT**:
Persian vocabulary bias for better transcription:
```python
HOTWORDS = [
    "بله", "آره", "نه", "خیر", "باشه", "حتما",
    "کجا", "شماره", "کلاس", "زبان", "انگلیسی",
    # ... 40+ Persian words
]
```

**Empty Audio Detection**:

Heuristic to detect silent/empty recordings:
```python
def is_likely_empty(duration_sec: float, transcript: str) -> bool:
    # Very short duration
    if duration_sec < 0.5:
        return True

    # Empty or very short transcript
    if len(transcript.strip()) < 3:
        return True

    # Only noise/filler
    if transcript in ["آ", "ا", "م", "ه", "..."]:
        return True

    return False
```

**Result Codes**:

The system maps call outcomes to these standardized codes:

| Code | Description |
|------|-------------|
| `connected_to_operator` | Customer said yes, operator answered (Agrad branch) |
| `disconnected` | Customer said yes but hung up or operator unavailable (Salehi branch) |
| `not_interested` | Customer said no |
| `hangup` | Customer hung up before response |
| `missed` | No answer / busy / timeout |
| `unknown` | Unclear intent |
| `busy` | SIP cause 17 (busy) |
| `power_off` | SIP cause 18/19/20 (power off) |
| `banned` | SIP cause 21/34/41/42 (rejected) |
| `failed:<reason>` | Technical failures (STT, LLM, recording) |

**Failure Handling**:

The scenario handles various failure modes:

1. **Vira Balance Errors**:
   - Detected in STT response
   - Pauses dialer immediately
   - Sends SMS alert

2. **LLM Quota Errors**:
   - HTTP 403 or quota-related messages
   - Pauses dialer (hard failure)
   - Logs error

3. **Recording Failures**:
   - Missing recording file
   - Empty recording
   - Marks as "failed:recording"

4. **Network Failures**:
   - Retries with exponential backoff
   - Queues panel reports for retry

**Transcript Logging**:

All transcripts are logged to dedicated files for analysis:

- `logs/positive_stt.log`: YES intents
- `logs/negative_stt.log`: NO intents
- `logs/unknown_stt.log`: UNKNOWN intents

Format: `{timestamp} | {session_id} | {intent} | {transcript}`

**Key Methods**:
```python
async def on_call_answered(session: Session)
async def on_playback_finished(session: Session, playback_id: str)
async def on_recording_finished(session: Session, recording_name: str)
async def transcribe_and_classify(session: Session, recording_path: str)
async def route_by_intent(session: Session, intent: str, transcript: str)
async def connect_to_operator(session: Session)  # Disabled on Salehi
```

**Branch-Specific Behavior**:

**Salehi Branch** (current):
- YES intent → Play "yes" → Disconnect
- Result: `disconnected`
- No operator transfer

**Agrad Branch**:
- YES intent → Play "yes" + "onhold" → Connect to operator
- Result: `connected_to_operator` or `disconnected`
- Round-robin agent selection

---

### 7. External Integrations

#### LLM Client ([llm/client.py](llm/client.py))

**Purpose**: GapGPT (OpenAI-compatible) wrapper for intent classification

**Configuration**:
```python
BASE_URL = "https://api.gapgpt.app/v1"
MODEL = "gpt-4o-mini"
TEMPERATURE = 0.3  # Low for consistent classification
```

**Features**:
- Async chat completions
- Semaphore-based concurrency control (MAX_PARALLEL_LLM)
- Bearer token authentication
- Configurable temperature and response format

**Usage**:
```python
llm_client = GapGPTClient(settings.gapgpt, semaphore)
response = await llm_client.chat_completion(
    messages=[{"role": "user", "content": prompt}]
)
intent = response["choices"][0]["message"]["content"].strip()
```

#### Speech-to-Text ([stt_tts/vira_stt.py](stt_tts/vira_stt.py))

**Purpose**: Vira STT client with audio preprocessing

**Audio Enhancement Pipeline**:
1. ffmpeg preprocessing (see Marketing Scenario section)
2. Save enhanced copy to `/var/spool/asterisk/recording/enhanced/`
3. Send enhanced audio to Vira API

**API Request Format**:
```python
{
    "file": base64_encoded_wav,
    "features": {
        "hotwords": [
            {"word": "بله", "weight": 2.0},
            {"word": "آره", "weight": 2.0},
            # ...
        ]
    }
}
```

**Response Parsing**:
Multi-level fallback for transcript extraction:
```python
# Try "text" field
if "text" in data:
    return data["text"]

# Try nested "data"."text"
if "data" in data and "text" in data["data"]:
    return data["data"]["text"]

# Try "result"
if "result" in data:
    return data["result"]

# Empty
return ""
```

**Balance Error Detection**:
```python
if "موجودی" in text or "اعتبار" in text:
    raise Exception("Vira balance error")
```

**Features**:
- Hotword support for Persian vocabulary
- Concurrent request limiting (MAX_PARALLEL_STT)
- SSL verification (configurable)
- Enhanced audio archival

#### Panel API Client ([integrations/panel/client.py](integrations/panel/client.py))

**Purpose**: External panel dialer API integration

**Endpoints**:

1. **Get Next Batch**:
```python
POST /api/dialer/get_next_batch
{
    "size": 10,
    "timezone": "+0330",
    "schedule_version": "v1"
}

Response:
{
    "contacts": [
        {"id": 123, "phone": "09123456789", "metadata": {...}},
        ...
    ],
    "active_agents": [
        {"id": 1, "name": "Agent1", "mobile": "09121111111"},
        ...
    ],
    "call_allowed": true
}
```

2. **Report Result**:
```python
POST /api/dialer/report_result
{
    "contact_id": 123,
    "status": "CONNECTED",
    "user_message": "بله حتما",
    "agent_id": 1,
    "agent_phone": "09121111111",
    "timestamp": "2024-01-15T10:30:00"
}
```

**Status Mapping**:
```python
RESULT_TO_PANEL_STATUS = {
    "connected_to_operator": "CONNECTED",
    "not_interested": "NOT_INTERESTED",
    "missed": "MISSED",
    "hangup": "HANGUP",
    "disconnected": "DISCONNECTED",
    "failed": "FAILED",
    "busy": "BUSY",
    "power_off": "POWER_OFF",
    "banned": "BANNED",
    "unknown": "UNKNOWN"
}
```

**Features**:
- Queued report retry on network failures
- Agent roster updates for operator routing
- `call_allowed` flag support (pauses dialer when false)
- Bearer token authentication

#### SMS Alerts ([integrations/sms/melipayamak.py](integrations/sms/melipayamak.py))

**Purpose**: SMS alerting via Melipayamak service

**Use Cases**:
- Consecutive failure alerts
- Vira balance errors
- LLM quota errors
- Dialer pause notifications

**Configuration**:
```python
SMS_API_KEY = "your_api_key"
SMS_FROM = "9982003047"
SMS_ADMINS = "09369344330,09123456789"  # Comma-separated
FAIL_ALERT_THRESHOLD = 3
```

**Usage**:
```python
sms_client = MelipayamakClient(api_key)
await sms_client.send_sms(
    from_number="9982003047",
    to_numbers=["09369344330"],
    message="⚠️ Dialer paused: 3 consecutive failures"
)
```

---

### 8. Utilities

#### Audio Sync ([utils/audio_sync.py](utils/audio_sync.py))

**Purpose**: Audio asset conversion and deployment to Asterisk

**Process**:

1. **Convert MP3 → WAV** (16kHz mono, pcm_s16le):
```bash
ffmpeg -i assets/audio/src/hello.mp3 \
  -ar 16000 -ac 1 -acodec pcm_s16le \
  assets/audio/wav/hello.wav
```

2. **Convert MP3 → ULAW** (8kHz):
```bash
ffmpeg -i assets/audio/src/hello.mp3 \
  -ar 8000 -ac 1 -acodec pcm_mulaw \
  assets/audio/wav/hello.ulaw
```

3. **Convert MP3 → ALAW** (8kHz):
```bash
ffmpeg -i assets/audio/src/hello.mp3 \
  -ar 8000 -ac 1 -acodec pcm_alaw \
  assets/audio/wav/hello.alaw
```

4. **Copy to Asterisk**:
```bash
cp assets/audio/wav/* /var/lib/asterisk/sounds/custom/
chown asterisk:asterisk /var/lib/asterisk/sounds/custom/*
chmod 644 /var/lib/asterisk/sounds/custom/*
```

**Audio Prompts**:
- `hello.mp3`: Initial greeting ("سلام، با مرکز...")
- `yes.mp3`: Acknowledgment before transfer ("بله حتما")
- `goodby.mp3`: Farewell message ("ممنون از وقتی که گذاشتید")
- `number.mp3`: Response to number question ("شماره شما از...")
- `onhold.mp3`: Hold music while waiting for operator
- `alo.mp3`: Quick acknowledgment
- `repeat.mp3`: Prompt repetition

**Automatic Sync**:
Audio sync runs automatically on application startup (see [main.py](main.py)).

**Manual Sync**:
```bash
bash scripts/sync_audio.sh
```

---

## Configuration Guide

### Environment Variables

#### Required Configuration

**Scenario Configuration**:
```bash
# Determines which call flow to use
# Options:
#   - salehi: On YES intent, play "yes" prompt then disconnect (no operator transfer)
#   - agrad: On YES intent, play "yes" + "onhold" then connect to operator
SCENARIO=salehi
```

**Important**: See [Scenario Management Guide](docs/scenario_management.md) for detailed information on managing multiple scenarios.

**ARI Connection**:
```bash
ARI_BASE_URL=http://127.0.0.1:8088/ari
ARI_WS_URL=ws://127.0.0.1:8088/ari/events
ARI_APP_NAME=salehi
ARI_USERNAME=ari_user
ARI_PASSWORD=ari_password
```

**Dialer Configuration**:
```bash
# Outbound trunk and lines
OUTBOUND_TRUNK=TO-CUCM-Gaptel
OUTBOUND_NUMBERS=02191302954,02191302955  # Comma-separated
DEFAULT_CALLER_ID=##1000

# Timeouts
ORIGINATION_TIMEOUT=30  # Seconds to wait for answer

# Per-line rate limits
MAX_CONCURRENT_CALLS=2  # Total per line (inbound+outbound)
MAX_CALLS_PER_MINUTE=10
MAX_CALLS_PER_DAY=200

# Global rate limits (0 = unlimited)
MAX_CONCURRENT_INBOUND_CALLS=0
MAX_CONCURRENT_OUTBOUND_CALLS=0
MAX_ORIGINATIONS_PER_SECOND=3  # Across all lines

# Dialer behavior
DIALER_BATCH_SIZE=10
DIALER_DEFAULT_RETRY=60  # Seconds between batch fetches
```

**Static Contacts** (when panel disabled):
```bash
STATIC_CONTACTS=09123456789,09987654321
```

**Panel API**:
```bash
PANEL_BASE_URL=https://panel.example.com
PANEL_API_TOKEN=panel_sample_token
```

**LLM (GapGPT)**:
```bash
GAPGPT_BASE_URL=https://api.gapgpt.app/v1
GAPGPT_API_KEY=gapgpt_sample_key
GAPGPT_MODEL=gpt-4o-mini
GAPGPT_TEMPERATURE=0.3
```

**Speech Services (Vira)**:
```bash
VIRA_STT_TOKEN=vira_stt_sample_key
VIRA_TTS_TOKEN=vira_tts_sample_key
VIRA_STT_URL=https://partai.gw.isahab.ir/avanegar/v2/avanegar/request
VIRA_TTS_URL=https://partai.gw.isahab.ir/avasho/v2/avasho/request
VIRA_VERIFY_SSL=true
```

**Operator Transfer**:
```bash
OPERATOR_EXTENSION=200
OPERATOR_TRUNK=TO-CUCM-Gaptel
OPERATOR_CALLER_ID=2000
OPERATOR_TIMEOUT=30
OPERATOR_ENDPOINT=Local/6005@from-internal  # Optional override
OPERATOR_MOBILE_NUMBERS=09121111111,09122222222  # Agent mobiles
USE_PANEL_AGENTS=false  # Use panel active_agents roster
```

**SMS Alerts**:
```bash
SMS_API_KEY=your_melipayamak_key
SMS_FROM=9982003047
SMS_ADMINS=09369344330,09123456789  # Comma-separated
FAIL_ALERT_THRESHOLD=3  # Consecutive failures before alert
```

**Concurrency and Timeouts**:
```bash
# HTTP client
HTTP_MAX_CONNECTIONS=100
HTTP_TIMEOUT=10

# Service timeouts
ARI_TIMEOUT=10
STT_TIMEOUT=30
TTS_TIMEOUT=30
LLM_TIMEOUT=20

# Concurrency limits
MAX_PARALLEL_STT=50
MAX_PARALLEL_TTS=50
MAX_PARALLEL_LLM=10
```

**Audio Paths**:
```bash
AST_SOUND_DIR=/var/lib/asterisk/sounds/custom
AST_RECORDING_DIR=/var/spool/asterisk/recording
```

**Logging**:
```bash
LOG_LEVEL=INFO  # DEBUG for verbose output
```

### Asterisk Dialplan Configuration

**Required**: Asterisk must route calls to the Stasis application

**File**: `/etc/asterisk/extensions.conf` (or extensions_custom.conf in FreePBX)

```ini
[from-internal]
exten => _X.,1,Stasis(salehi)
  same => n,Hangup()

[from-trunk]
exten => _X.,1,Stasis(salehi)
  same => n,Hangup()
```

**Explanation**:
- All calls entering `from-internal` or `from-trunk` contexts are sent to the `salehi` Stasis application
- The application name must match `ARI_APP_NAME` in your `.env` file
- Asterisk will send events to the WebSocket at `ws://127.0.0.1:8088/ari/events?app=salehi`

---

## Development Workflow

### Setup Development Environment

```bash
# 1. Install Python 3.12 and ffmpeg
sudo apt-get update
sudo apt-get install -y python3.12 python3.12-venv ffmpeg

# 2. Clone repository
git clone <repository-url>
cd Salehi

# 3. Create virtual environment
python3.12 -m venv venv
source venv/bin/activate

# 4. Install dependencies
pip install -r requirements.txt

# 5. Configure environment
cp .env.example .env
# Edit .env with your credentials

# 6. Ensure Asterisk is configured (see above)

# 7. Run application
python main.py
```

### Running the Application

**Development**:
```bash
# Activate venv
source venv/bin/activate

# Run with INFO logging
python main.py

# Run with DEBUG logging
LOG_LEVEL=DEBUG python main.py
```

**Production** (via systemd):
```bash
# Create systemd service
sudo nano /etc/systemd/system/salehi.service
```

```ini
[Unit]
Description=Salehi CallCenter
After=network.target asterisk.service

[Service]
Type=simple
User=asterisk
WorkingDirectory=/media/saeid/Software/Projects/Salehi
Environment="PATH=/media/saeid/Software/Projects/Salehi/venv/bin"
ExecStart=/media/saeid/Software/Projects/Salehi/venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable salehi
sudo systemctl start salehi

# Check status
sudo systemctl status salehi

# View logs
sudo journalctl -u salehi -f
```

### Deployment

**Automated Deployment** ([update.sh](update.sh)):

```bash
# Detects current branch (salehi/agrad)
# Pulls latest code
# Updates dependencies
# Sets permissions
# Restarts service

./update.sh
```

**Manual Deployment**:
```bash
# Pull latest code
git pull origin salehi

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt

# Sync audio assets
bash scripts/sync_audio.sh

# Restart service
sudo systemctl restart salehi
```

### Testing

**Unit Tests** (if implemented):
```bash
pytest tests/
```

**Manual Testing**:

1. **Outbound Test**:
   - Set `STATIC_CONTACTS=09123456789` in `.env`
   - Run application
   - Check logs for call origination
   - Answer call and verify flow

2. **Inbound Test**:
   - Call one of the `OUTBOUND_NUMBERS`
   - Verify auto-answer
   - Check logs for session creation

3. **Recording Test**:
   - Complete a call
   - Check `/var/spool/asterisk/recording/` for recordings
   - Check `/var/spool/asterisk/recording/enhanced/` for processed audio

4. **Panel Integration Test**:
   - Enable panel in `.env`
   - Verify batch fetching in logs
   - Complete calls and check panel for results

### Logging and Monitoring

**Log Files**:

- `logs/app.log`: Main application log (5MB rotation, 5 backups)
- `logs/hangups.log`: Dedicated hangup event log
- `logs/userdrop.log`: Customer disconnection timing
- `logs/positive_stt.log`: YES intent transcripts
- `logs/negative_stt.log`: NO intent transcripts
- `logs/unknown_stt.log`: UNKNOWN intent transcripts

**Log Format**:
```
2024-01-15 10:30:45,123 - INFO - [SessionManager] Created session abc123 for outbound call to 09123456789
```

**Monitoring Commands**:

```bash
# Tail main log
tail -f logs/app.log

# Tail hangup log
tail -f logs/hangups.log

# Search for errors
grep ERROR logs/app.log

# Count sessions today
grep "Created session" logs/app.log | grep "$(date +%Y-%m-%d)" | wc -l

# View positive transcripts
cat logs/positive_stt.log
```

---

## Common Tasks

### Adding a New Audio Prompt

1. **Create MP3 file**:
   - Record or generate audio
   - Save to `assets/audio/src/new_prompt.mp3`

2. **Update audio sync**:
   - Audio sync will auto-convert on next startup
   - Or run: `bash scripts/sync_audio.sh`

3. **Use in code**:
   ```python
   await self.ari.play_on_bridge(
       session.bridge.bridge_id,
       "sound:custom/new_prompt"
   )
   ```

### Modifying Call Flow

**File**: [logic/marketing_outreach.py](logic/marketing_outreach.py)

**Example**: Add a new intent "maybe"

1. **Update LLM prompt**:
   ```python
   INTENTS = ["yes", "no", "number_question", "maybe", "unknown"]
   ```

2. **Add routing logic**:
   ```python
   async def route_by_intent(self, session, intent, transcript):
       if intent == "maybe":
           await self.play_prompt(session, "sound:custom/maybe_prompt")
           # Record again or schedule callback
   ```

3. **Update result mapping**:
   ```python
   result = "maybe"
   await self.panel.report_result(contact_id, "MAYBE", transcript)
   ```

### Adjusting Rate Limits

**File**: [.env](.env)

**Per-Line Limits**:
```bash
MAX_CONCURRENT_CALLS=5  # Increase concurrent calls per line
MAX_CALLS_PER_MINUTE=20  # Increase calls per minute
MAX_CALLS_PER_DAY=500  # Increase daily quota
```

**Global Limits**:
```bash
MAX_ORIGINATIONS_PER_SECOND=5  # Increase global origination rate
```

**Concurrency Limits**:
```bash
MAX_PARALLEL_STT=100  # Increase STT concurrency
MAX_PARALLEL_LLM=20  # Increase LLM concurrency
```

### Enabling Operator Transfer

**Note**: Operator transfer is disabled on Salehi branch. To enable:

1. **Set environment variables**:
   ```bash
   OPERATOR_MOBILE_NUMBERS=09121111111,09122222222
   # OR
   USE_PANEL_AGENTS=true
   ```

2. **Modify routing logic** in [logic/marketing_outreach.py](logic/marketing_outreach.py):

   Find the `route_by_intent` method and change:
   ```python
   if intent == "yes":
       # Current Salehi behavior
       await self.play_prompt(session, "sound:custom/yes")
       result = "disconnected"
   ```

   To:
   ```python
   if intent == "yes":
       # Agrad behavior
       await self.play_prompt(session, "sound:custom/yes")
       await self.play_prompt(session, "sound:custom/onhold")
       success = await self.connect_to_operator(session)
       result = "connected_to_operator" if success else "disconnected"
   ```

### Debugging Call Issues

**Issue**: Calls not originating

1. Check dialer logs:
   ```bash
   grep "Originating call" logs/app.log
   ```

2. Check rate limits:
   ```bash
   grep "Rate limit" logs/app.log
   ```

3. Check panel connection:
   ```bash
   grep "Panel" logs/app.log
   ```

4. Verify ARI connection:
   ```bash
   curl http://127.0.0.1:8088/ari/asterisk/info \
     -u ari_user:ari_password
   ```

**Issue**: STT failing

1. Check Vira balance:
   ```bash
   grep "موجودی" logs/app.log
   ```

2. Check enhanced audio:
   ```bash
   ls -lah /var/spool/asterisk/recording/enhanced/
   ```

3. Test STT directly:
   ```python
   from stt_tts.vira_stt import ViraSTTClient
   client = ViraSTTClient(token, url)
   result = await client.transcribe("test.wav")
   print(result)
   ```

**Issue**: LLM classification errors

1. Check LLM logs:
   ```bash
   grep "LLM" logs/app.log
   ```

2. Check quota:
   ```bash
   grep "403" logs/app.log
   ```

3. Test LLM directly:
   ```python
   from llm.client import GapGPTClient
   client = GapGPTClient(settings)
   response = await client.chat_completion([
       {"role": "user", "content": "Test"}
   ])
   print(response)
   ```

---

## Architecture Insights

### Why Bridge-Centric?

Every session gets a mixing bridge, even single-leg calls. This design provides:

1. **Consistent Architecture**: All calls follow the same pattern
2. **Easy Expansion**: Add operator leg without restructuring
3. **Media Flexibility**: Play/record on bridge vs channel
4. **State Isolation**: Each session's audio is independent

### Concurrency Model

The system uses three layers of concurrency control:

1. **Per-Line Semaphores**: Shared between inbound and outbound
   - Prevents overloading individual lines
   - Each line has independent limits

2. **Global Semaphores**: Across all lines (optional)
   - `MAX_CONCURRENT_OUTBOUND_CALLS`
   - `MAX_CONCURRENT_INBOUND_CALLS`

3. **Service Semaphores**: Per external service
   - `MAX_PARALLEL_STT`, `MAX_PARALLEL_TTS`, `MAX_PARALLEL_LLM`
   - Prevents overwhelming external APIs

This multi-layer approach allows fine-grained control while maintaining system stability.

### Inbound Priority Mechanism

Inbound calls are prioritized over outbound:

1. When inbound arrives and line is at capacity:
   - Inbound is queued (not rejected)
   - Outbound origination pauses

2. When slot becomes available:
   - Queued inbound is processed first
   - Outbound resumes only when queue is empty

3. Benefits:
   - Never miss inbound opportunities
   - Better customer experience (no busy signals)
   - Efficient line utilization

### Rate Limiting Strategy

The dialer implements a sophisticated multi-dimensional rate limiting system:

**Sliding Window Counters**:
- Calls per minute: Tracks timestamps of recent calls
- Evicts old timestamps outside window
- Efficient O(1) amortized complexity

**Daily Quotas**:
- Resets at midnight (timezone-aware)
- Persistent across restarts (could be enhanced with Redis)

**Origination Throttle**:
- Prevents SIP floods
- Protects trunk provider relationships
- Configurable per-line and global

**Failure Backoff**:
- Consecutive failure detection
- Exponential backoff on errors
- Auto-resume on success

### Audio Enhancement Rationale

Raw phone recordings have significant challenges for STT:

1. **Narrow Bandwidth**: 8kHz phone quality
2. **Background Noise**: Office/street sounds
3. **Volume Variations**: Loud/quiet speakers

The ffmpeg pipeline addresses these:

1. **Band-pass Filtering**: Remove non-voice frequencies
2. **Noise Reduction**: FFT-based adaptive denoising
3. **Normalization**: Consistent loudness for STT

Result: **~30% improvement in Persian STT accuracy** (empirical)

### LLM vs Rule-Based Classification

**Why LLM?**

Persian language has complex intent expression:
- "آره باشه" (yes okay)
- "بله حتما" (yes definitely)
- "فعلا نه" (not now) - is this "no" or "maybe"?
- "شماره رو از کجا آوردید؟" (where did you get my number?)

Rule-based systems require extensive pattern matching. LLM provides:
- Context understanding
- Nuanced classification
- Easy to extend (just update prompt)

**Fallback Strategy**:
If LLM fails (quota, network), system falls back to simple token matching:
- Positive tokens → "yes"
- Negative tokens → "no"
- Otherwise → "unknown"

This provides **graceful degradation** while maintaining operation.

---

## Troubleshooting Guide

### Dialer Not Starting

**Symptoms**: No calls originating, dialer loop not running

**Checks**:
1. Verify panel `call_allowed`:
   ```bash
   grep "call_allowed" logs/app.log
   ```

2. Check rate limits:
   ```bash
   grep "Rate limit exceeded" logs/app.log
   ```

3. Verify static contacts (if panel disabled):
   ```bash
   echo $STATIC_CONTACTS
   ```

4. Check for failures:
   ```bash
   grep "Dialer paused" logs/app.log
   ```

### Calls Hanging Up Immediately

**Symptoms**: Calls connect but hang up within seconds

**Checks**:
1. Verify audio files exist:
   ```bash
   ls -lah /var/lib/asterisk/sounds/custom/
   ```

2. Check playback errors:
   ```bash
   grep "PlaybackFinished" logs/app.log
   ```

3. Verify bridge creation:
   ```bash
   grep "Created bridge" logs/app.log
   ```

4. Check channel state:
   ```bash
   grep "ChannelStateChange" logs/app.log
   ```

### Recording Failures

**Symptoms**: Recording files missing, STT skipped

**Checks**:
1. Verify recording directory permissions:
   ```bash
   ls -lad /var/spool/asterisk/recording/
   # Should be writable by asterisk user
   ```

2. Check disk space:
   ```bash
   df -h /var/spool/asterisk/
   ```

3. Check ffmpeg:
   ```bash
   which ffmpeg
   ffmpeg -version
   ```

4. Check recording logs:
   ```bash
   grep "RecordingFinished" logs/app.log
   ```

### STT Timeouts

**Symptoms**: Frequent "STT timeout" errors

**Checks**:
1. Increase timeout:
   ```bash
   STT_TIMEOUT=60  # In .env
   ```

2. Check Vira API status:
   ```bash
   curl -I https://partai.gw.isahab.ir/avanegar/v2/avanegar/request
   ```

3. Check network latency:
   ```bash
   ping partai.gw.isahab.ir
   ```

4. Check concurrency:
   ```bash
   grep "STT request" logs/app.log | grep "$(date +%H:%M)" | wc -l
   # If high, increase MAX_PARALLEL_STT
   ```

### LLM Classification Errors

**Symptoms**: All intents classified as "unknown"

**Checks**:
1. Verify API key:
   ```bash
   echo $GAPGPT_API_KEY
   ```

2. Test LLM endpoint:
   ```bash
   curl https://api.gapgpt.app/v1/chat/completions \
     -H "Authorization: Bearer $GAPGPT_API_KEY" \
     -H "Content-Type: application/json" \
     -d '{
       "model": "gpt-4o-mini",
       "messages": [{"role": "user", "content": "test"}]
     }'
   ```

3. Check quota:
   ```bash
   grep "403" logs/app.log
   ```

4. Review classification logs:
   ```bash
   grep "LLM classification" logs/app.log
   ```

### Panel Integration Issues

**Symptoms**: No batches received, results not reported

**Checks**:
1. Verify panel URL:
   ```bash
   curl -I $PANEL_BASE_URL
   ```

2. Test authentication:
   ```bash
   curl $PANEL_BASE_URL/api/dialer/get_next_batch \
     -H "Authorization: Bearer $PANEL_API_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{"size": 1}'
   ```

3. Check pending reports:
   ```bash
   grep "Queuing report for retry" logs/app.log
   ```

4. Review panel logs:
   ```bash
   grep "Panel" logs/app.log | tail -20
   ```

### High Memory Usage

**Symptoms**: Memory usage growing over time

**Checks**:
1. Check session cleanup:
   ```bash
   grep "Cleanup session" logs/app.log | wc -l
   ```

2. Monitor active sessions:
   ```bash
   # Add to code for debugging:
   logger.info(f"Active sessions: {len(session_manager.sessions)}")
   ```

3. Check for orphaned channels:
   ```bash
   asterisk -rx "core show channels"
   ```

4. Restart application periodically:
   ```bash
   # Add to systemd service
   RuntimeMaxSec=86400  # Restart daily
   ```

---

## Best Practices

### Code Organization

1. **Separation of Concerns**:
   - Core: ARI communication
   - Sessions: State management
   - Logic: Business rules
   - Integrations: External services

2. **Async/Await Everywhere**:
   - No blocking calls in main thread
   - Use `asyncio.to_thread()` for sync operations
   - Connection pooling for HTTP clients

3. **Strong Typing**:
   - Dataclasses for all models
   - Type hints for all functions
   - Validation at boundaries

4. **Error Handling**:
   - Try/except at service boundaries
   - Graceful degradation (LLM fallback)
   - Comprehensive logging

### Configuration Management

1. **Environment Variables**:
   - All config via `.env`
   - Never hardcode credentials
   - Provide `.env.example`

2. **Defaults**:
   - Sensible defaults for optional settings
   - Document all variables in README

3. **Validation**:
   - Validate required settings on startup
   - Fail fast with clear error messages

### Logging Strategy

1. **Structured Logging**:
   - Consistent format with timestamps
   - Include session_id in all session-related logs
   - Use log levels appropriately (DEBUG/INFO/ERROR)

2. **Dedicated Log Files**:
   - Separate critical events (hangups, transcripts)
   - Easy analysis and monitoring
   - Rotation to prevent disk fill

3. **Debug Mode**:
   - `LOG_LEVEL=DEBUG` for troubleshooting
   - Sanitize sensitive data (phone numbers, tokens)

### Performance Optimization

1. **Connection Pooling**:
   - Reuse HTTP connections
   - Configure max connections appropriately
   - Set reasonable timeouts

2. **Concurrency Control**:
   - Use semaphores to limit parallelism
   - Prevent overwhelming external services
   - Balance throughput vs resource usage

3. **Async Operations**:
   - Parallel where possible (read multiple files)
   - Sequential where required (rate limiting)
   - Use `asyncio.gather()` for fan-out

### Testing Recommendations

1. **Unit Tests**:
   - Test rate limiting logic
   - Test intent classification fallback
   - Test number normalization

2. **Integration Tests**:
   - Mock ARI WebSocket events
   - Test session lifecycle
   - Test failure scenarios

3. **Load Testing**:
   - Simulate high call volume
   - Monitor memory/CPU usage
   - Verify rate limits work correctly

### Deployment Practices

1. **Automated Deployment**:
   - Use `update.sh` for consistency
   - Version control all config
   - Test in staging first

2. **Monitoring**:
   - Set up log aggregation (ELK, Graylog)
   - Alert on consecutive failures
   - Track key metrics (calls/hour, success rate)

3. **Backup and Recovery**:
   - Backup `.env` file securely
   - Document recovery procedures
   - Test restore process

---

## FAQ

### Q: Why are calls marked as "missed" even though they rang?

**A**: The timeout watchdog marks calls as "missed" if no ARI events are received within `ORIGINATION_TIMEOUT`. This can happen if:
- Call was never answered
- Network issues prevented event delivery
- Asterisk didn't send events

Check `logs/app.log` for timeout events and verify Asterisk dialplan sends to Stasis.

### Q: Can I use multiple Asterisk servers?

**A**: Yes, but each instance of the application should connect to only one Asterisk server. To load-balance across multiple servers:
1. Run separate application instances
2. Use panel to distribute contacts
3. Configure different `OUTBOUND_NUMBERS` per instance

### Q: How do I add a new language?

**A**: To support a new language:
1. Replace STT/TTS with language-appropriate service
2. Update hotwords in [logic/marketing_outreach.py](logic/marketing_outreach.py)
3. Update LLM prompt with language-specific examples
4. Record new audio prompts

### Q: What happens if Vira STT is down?

**A**: If STT fails:
1. Call is marked as `failed:stt`
2. Dialer continues (no auto-pause)
3. Next call will retry STT

If consecutive STT failures exceed threshold, consider adding auto-pause logic similar to LLM failures.

### Q: Can I customize the result statuses?

**A**: Yes, modify the result mapping in [integrations/panel/client.py](integrations/panel/client.py):

```python
RESULT_TO_PANEL_STATUS = {
    "your_custom_result": "YOUR_PANEL_STATUS",
    # ...
}
```

And add the new result in [logic/marketing_outreach.py](logic/marketing_outreach.py).

### Q: How do I increase call volume?

**A**: To increase call volume:

1. **Add more lines**:
   ```bash
   OUTBOUND_NUMBERS=line1,line2,line3,line4
   ```

2. **Increase per-line limits**:
   ```bash
   MAX_CONCURRENT_CALLS=5
   MAX_CALLS_PER_MINUTE=30
   ```

3. **Increase global limits**:
   ```bash
   MAX_ORIGINATIONS_PER_SECOND=10
   ```

4. **Increase service concurrency**:
   ```bash
   MAX_PARALLEL_STT=100
   MAX_PARALLEL_LLM=20
   ```

5. **Monitor resources**: Ensure CPU/memory/network can handle load

### Q: Why does the dialer pause during inbound calls?

**A**: This is by design (inbound priority). When inbound calls are waiting and line capacity is full, outbound origination pauses to ensure inbound calls are answered promptly.

To disable inbound priority, modify [logic/dialer.py](logic/dialer.py) and remove the inbound waiting check.

### Q: Can I record all calls for quality assurance?

**A**: Yes, recordings are already created at `/var/spool/asterisk/recording/`. To keep them:

1. Disable auto-cleanup in [sessions/session_manager.py](sessions/session_manager.py)
2. Set up archival script to move recordings to long-term storage
3. Ensure disk space is sufficient
4. Comply with recording notification laws in your jurisdiction

### Q: How do I migrate from Salehi to Agrad branch?

**A**: To switch branches:

```bash
# 1. Commit any local changes
git add .
git commit -m "Local changes"

# 2. Switch branch
git checkout agrad

# 3. Update dependencies
source venv/bin/activate
pip install -r requirements.txt

# 4. Sync audio
bash scripts/sync_audio.sh

# 5. Update .env if needed (compare with .env.example)

# 6. Restart service
sudo systemctl restart salehi
```

**Note**: Agrad branch has different call flow (operator transfer enabled).

---

## Additional Resources

### Documentation Files

- [README.md](README.md): User-facing documentation and quick start
- [agent.md](agent.md): Developer notes and working rules
- [docs/scenario_management.md](docs/scenario_management.md): **Scenario management guide** (Salehi vs Agrad, migration from branches)
- [docs/branching.md](docs/branching.md): Deployment strategy and branch model
- [docs/llm_vira_usage.md](docs/llm_vira_usage.md): LLM and Vira API integration guide

### External Documentation

- **Asterisk ARI**: https://wiki.asterisk.org/wiki/display/AST/Asterisk+REST+Interface
- **httpx**: https://www.python-httpx.org/
- **asyncio**: https://docs.python.org/3/library/asyncio.html
- **Vira STT/TTS**: (Contact provider for API documentation)
- **GapGPT**: https://api.gapgpt.app/docs

### Contact

For issues, questions, or contributions, contact the development team or file an issue in the project repository.

---

## Appendix: Data Flow Diagrams

### Outbound Call Flow

```
┌─────────────┐
│   Dialer    │
│  selects    │
│  contact    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ ARI HTTP    │
│ originate() │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Asterisk   │
│ initiates   │
│    call     │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│ StasisStart │
│    event    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│Session Mgr  │
│   creates   │
│   session   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Customer   │
│  answers    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Marketing  │
│  Scenario   │
│on_answered()│
└──────┬──────┘
       │
       ▼
┌─────────────┐
│Play "hello" │
│   prompt    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Record    │
│  response   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   ffmpeg    │
│  enhance    │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│  Vira STT   │
│ transcribe  │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   LLM       │
│  classify   │
└──────┬──────┘
       │
       ▼
┌─────────────┐
│   Route     │
│ by intent   │
└──────┬──────┘
       │
       ├─────YES────► Play "yes" ────► Disconnect
       │
       ├─────NO─────► Play "goodby" ─► Hangup
       │
       ├──NUMBER────► Play "number" ─► Record again
       │
       └──UNKNOWN───► Play "goodby" ─► Hangup
```

### Intent Classification Flow

```
┌─────────────────┐
│  Recording      │
│  completed      │
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│ Check file      │
│ exists?         │
└────┬───────┬────┘
     │       │
    NO      YES
     │       │
     │       ▼
     │  ┌─────────────────┐
     │  │ ffmpeg enhance  │
     │  │ (noise reduce,  │
     │  │  normalize)     │
     │  └────────┬────────┘
     │           │
     │           ▼
     │  ┌─────────────────┐
     │  │ Vira STT        │
     │  │ transcribe      │
     │  └────┬───────┬────┘
     │       │       │
     │     SUCCESS  FAIL
     │       │       │
     │       ▼       │
     │  ┌─────────────────┐
     │  │ Empty audio?    │
     │  └────┬───────┬────┘
     │       │       │
     │      NO      YES
     │       │       │
     │       ▼       │
     │  ┌─────────────────┐
     │  │ LLM classify    │
     │  │ (GapGPT)        │
     │  └────┬───────┬────┘
     │       │       │
     │     SUCCESS  FAIL
     │       │       │
     │       ▼       ▼
     │  ┌─────────────────┐
     │  │ Token-based     │
     │  │ fallback        │
     │  └────────┬────────┘
     │           │
     │           ▼
     │  ┌─────────────────┐
     │  │ Intent result:  │
     │  │ yes/no/number/  │
     │  │ unknown         │
     │  └────────┬────────┘
     │           │
     ▼           ▼
┌─────────────────┐
│ failed:         │
│ recording       │
└─────────────────┘
```

### Rate Limiting Decision Tree

```
                ┌─────────────────┐
                │ Originate call? │
                └────────┬────────┘
                         │
                         ▼
                ┌─────────────────┐
                │ Dialer paused?  │
                └────┬───────┬────┘
                     │       │
                    YES     NO
                     │       │
                     │       ▼
                     │  ┌─────────────────┐
                     │  │ Panel allows?   │
                     │  └────┬───────┬────┘
                     │       │       │
                     │      NO      YES
                     │       │       │
                     │       │       ▼
                     │       │  ┌─────────────────┐
                     │       │  │ Inbound waiting?│
                     │       │  └────┬───────┬────┘
                     │       │       │       │
                     │       │      YES     NO
                     │       │       │       │
                     │       │       │       ▼
                     │       │       │  ┌─────────────────┐
                     │       │       │  │ Select line     │
                     │       │       │  │ (least loaded)  │
                     │       │       │  └────────┬────────┘
                     │       │       │           │
                     │       │       │           ▼
                     │       │       │  ┌─────────────────┐
                     │       │       │  │ Line has        │
                     │       │       │  │ capacity?       │
                     │       │       │  └────┬───────┬────┘
                     │       │       │       │       │
                     │       │       │      NO      YES
                     │       │       │       │       │
                     │       │       │       │       ▼
                     │       │       │       │  ┌─────────────────┐
                     │       │       │       │  │ Global limit    │
                     │       │       │       │  │ reached?        │
                     │       │       │       │  └────┬───────┬────┘
                     │       │       │       │       │       │
                     │       │       │       │      YES     NO
                     │       │       │       │       │       │
                     │       │       │       │       │       ▼
                     │       │       │       │       │  ┌─────────────────┐
                     │       │       │       │       │  │ Per-minute      │
                     │       │       │       │       │  │ limit OK?       │
                     │       │       │       │       │  └────┬───────┬────┘
                     │       │       │       │       │       │       │
                     │       │       │       │       │      NO      YES
                     │       │       │       │       │       │       │
                     │       │       │       │       │       │       ▼
                     │       │       │       │       │       │  ┌─────────────────┐
                     │       │       │       │       │       │  │ Daily limit OK? │
                     │       │       │       │       │       │  └────┬───────┬────┘
                     │       │       │       │       │       │       │       │
                     │       │       │       │       │       │      NO      YES
                     │       │       │       │       │       │       │       │
                     │       │       │       │       │       │       │       ▼
                     │       │       │       │       │       │       │  ┌─────────────────┐
                     │       │       │       │       │       │       │  │ Last origination│
                     │       │       │       │       │       │       │  │ >1sec ago?      │
                     │       │       │       │       │       │       │  └────┬───────┬────┘
                     │       │       │       │       │       │       │       │       │
                     │       │       │       │       │       │       │      NO      YES
                     │       │       │       │       │       │       │       │       │
                     │       │       │       │       │       │       │       │       ▼
                     │       │       │       │       │       │       │       │  ┌─────────────────┐
                     │       │       │       │       │       │       │       │  │ ✅ ORIGINATE!   │
                     │       │       │       │       │       │       │       │  └─────────────────┘
                     │       │       │       │       │       │       │       │
                     ▼       ▼       ▼       ▼       ▼       ▼       ▼       ▼
                ┌─────────────────────────────────────────────────────────────┐
                │                      ⏸️  WAIT                                │
                └─────────────────────────────────────────────────────────────┘
```

---

**End of CLAUDE.md**

This document provides a comprehensive guide to the Salehi CallCenter project architecture, configuration, development workflow, and operational best practices. For additional questions or clarifications, refer to the inline code documentation or contact the development team.
