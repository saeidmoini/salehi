import os
from dataclasses import dataclass
from datetime import time
from typing import List


def _load_dotenv(path: str = ".env") -> None:
    """
    Minimal .env loader using only the standard library.
    Existing environment variables are not overridden.
    """
    if not os.path.exists(path):
        return

    with open(path, encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key not in os.environ:
                os.environ[key] = value


def _parse_time(value: str, default: time) -> time:
    try:
        hour, minute = value.split(":")
        return time(int(hour), int(minute))
    except Exception:
        return default


def _parse_list(value: str) -> List[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


@dataclass
class AriSettings:
    base_url: str
    ws_url: str
    app_name: str
    username: str
    password: str


@dataclass
class GapGPTSettings:
    base_url: str
    api_key: str


@dataclass
class ViraSettings:
    stt_token: str
    tts_token: str
    stt_url: str
    tts_url: str
    verify_ssl: bool


@dataclass
class AudioSettings:
    src_dir: str
    wav_dir: str
    ast_sound_dir: str


@dataclass
class OperatorSettings:
    extension: str
    trunk: str
    caller_id: str
    timeout: int
    endpoint: str
    mobile_numbers: List[str]
    use_panel_agents: bool


@dataclass
class DialerSettings:
    outbound_trunk: str
    outbound_numbers: List[str]
    default_caller_id: str
    origination_timeout: int
    max_concurrent_calls: int
    max_concurrent_outbound_calls: int
    max_concurrent_inbound_calls: int
    max_calls_per_minute: int
    max_calls_per_day: int
    max_originations_per_second: float
    call_window_start: time
    call_window_end: time
    static_contacts: List[str]
    batch_size: int
    default_retry: int


@dataclass
class ConcurrencySettings:
    max_parallel_stt: int
    max_parallel_tts: int
    max_parallel_llm: int
    http_max_connections: int


@dataclass
class TimeoutSettings:
    http_timeout: float
    stt_timeout: float
    tts_timeout: float
    llm_timeout: float
    ari_timeout: float


@dataclass
class SMSSettings:
    api_key: str
    sender: str
    admins: List[str]
    fail_alert_threshold: int


@dataclass
class ScenarioSettings:
    """
    Scenario configuration to support different call flows.

    Scenarios:
    - salehi: On YES intent, play "yes" prompt then disconnect (no operator transfer)
    - agrad: On YES intent, play "yes" + "onhold" then connect to operator
    """
    name: str  # "salehi" or "agrad"
    transfer_to_operator: bool  # Whether to transfer YES intents to operator
    audio_src_dir: str  # Scenario-specific audio source directory


@dataclass
class Settings:
    ari: AriSettings
    gapgpt: GapGPTSettings
    vira: ViraSettings
    dialer: DialerSettings
    operator: OperatorSettings
    panel: "PanelSettings"
    audio: AudioSettings
    concurrency: ConcurrencySettings
    timeouts: TimeoutSettings
    sms: SMSSettings
    scenario: ScenarioSettings
    log_level: str


@dataclass
class PanelSettings:
    base_url: str
    api_token: str


def get_settings() -> Settings:
    _load_dotenv()

    ari = AriSettings(
        base_url=os.getenv("ARI_BASE_URL", "http://127.0.0.1:8088/ari"),
        ws_url=os.getenv("ARI_WS_URL", "ws://127.0.0.1:8088/ari/events"),
        app_name=os.getenv("ARI_APP_NAME", "salehi"),
        username=os.getenv("ARI_USERNAME", "salehi"),
        password=os.getenv("ARI_PASSWORD", "changeme"),
    )

    gapgpt = GapGPTSettings(
        base_url=os.getenv("GAPGPT_BASE_URL", "https://api.gapgpt.app/v1"),
        api_key=os.getenv("GAPGPT_API_KEY", ""),
    )

    vira = ViraSettings(
        stt_token=os.getenv("VIRA_STT_TOKEN", ""),
        tts_token=os.getenv("VIRA_TTS_TOKEN", ""),
        stt_url=os.getenv(
            "VIRA_STT_URL", "https://partai.gw.isahab.ir/avanegar/v2/avanegar/request"
        ),
        tts_url=os.getenv(
            "VIRA_TTS_URL", "https://partai.gw.isahab.ir/avasho/v2/avasho/request"
        ),
        verify_ssl=os.getenv("VIRA_VERIFY_SSL", "true").lower() not in ("0", "false", "no"),
    )

    call_window_start = _parse_time(
        os.getenv("CALL_WINDOW_START", "00:00"), default=time(0, 0)
    )
    call_window_end = _parse_time(
        os.getenv("CALL_WINDOW_END", "23:59"), default=time(23, 59)
    )

    dialer = DialerSettings(
        outbound_trunk=os.getenv("OUTBOUND_TRUNK", "TO-CUCM-Gaptel"),
        outbound_numbers=_parse_list(os.getenv("OUTBOUND_NUMBERS", "")),
        default_caller_id=os.getenv("DEFAULT_CALLER_ID", "1000"),
        origination_timeout=int(os.getenv("ORIGINATION_TIMEOUT", "30")),
        max_concurrent_calls=int(os.getenv("MAX_CONCURRENT_CALLS", "2")),
        max_concurrent_outbound_calls=int(os.getenv("MAX_CONCURRENT_OUTBOUND_CALLS", os.getenv("MAX_CONCURRENT_CALLS", "2"))),
        max_concurrent_inbound_calls=int(os.getenv("MAX_CONCURRENT_INBOUND_CALLS", os.getenv("MAX_CONCURRENT_CALLS", "2"))),
        max_calls_per_minute=int(os.getenv("MAX_CALLS_PER_MINUTE", "10")),
        max_calls_per_day=int(os.getenv("MAX_CALLS_PER_DAY", "200")),
        max_originations_per_second=float(os.getenv("MAX_ORIGINATIONS_PER_SECOND", "3")),
        call_window_start=call_window_start,
        call_window_end=call_window_end,
        static_contacts=_parse_list(os.getenv("STATIC_CONTACTS", "")),
        batch_size=int(os.getenv("DIALER_BATCH_SIZE", os.getenv("MAX_CALLS_PER_MINUTE", "10"))),
        default_retry=int(os.getenv("DIALER_DEFAULT_RETRY", "60")),
    )

    operator = OperatorSettings(
        extension=os.getenv("OPERATOR_EXTENSION", "200"),
        trunk=os.getenv("OPERATOR_TRUNK", os.getenv("OUTBOUND_TRUNK", "TO-CUCM-Gaptel")),
        caller_id=os.getenv("OPERATOR_CALLER_ID", os.getenv("DEFAULT_CALLER_ID", "1000")),
        timeout=int(os.getenv("OPERATOR_TIMEOUT", "30")),
        endpoint=os.getenv("OPERATOR_ENDPOINT", ""),
        mobile_numbers=_parse_list(os.getenv("OPERATOR_MOBILE_NUMBERS", "")),
        use_panel_agents=os.getenv("USE_PANEL_AGENTS", "false").lower() == "true",
    )

    panel = PanelSettings(
        base_url=os.getenv("PANEL_BASE_URL", ""),
        api_token=os.getenv("PANEL_API_TOKEN", ""),
    )

    audio = AudioSettings(
        src_dir=os.getenv("AUDIO_SRC_DIR", "assets/audio/src"),
        wav_dir=os.getenv("AUDIO_WAV_DIR", "assets/audio/wav"),
        ast_sound_dir=os.getenv("AST_SOUND_DIR", "/var/lib/asterisk/sounds/custom"),
    )

    concurrency = ConcurrencySettings(
        max_parallel_stt=int(os.getenv("MAX_PARALLEL_STT", "50")),
        max_parallel_tts=int(os.getenv("MAX_PARALLEL_TTS", "50")),
        max_parallel_llm=int(os.getenv("MAX_PARALLEL_LLM", "10")),
        http_max_connections=int(os.getenv("HTTP_MAX_CONNECTIONS", "100")),
    )

    timeouts = TimeoutSettings(
        http_timeout=float(os.getenv("HTTP_TIMEOUT", "10")),
        stt_timeout=float(os.getenv("STT_TIMEOUT", "30")),
        tts_timeout=float(os.getenv("TTS_TIMEOUT", "30")),
        llm_timeout=float(os.getenv("LLM_TIMEOUT", "20")),
        ari_timeout=float(os.getenv("ARI_TIMEOUT", "10")),
    )

    sms = SMSSettings(
        api_key=os.getenv("SMS_API_KEY", ""),
        sender=os.getenv("SMS_FROM", ""),
        admins=_parse_list(os.getenv("SMS_ADMINS", "")),
        fail_alert_threshold=int(os.getenv("FAIL_ALERT_THRESHOLD", "3")),
    )

    # Scenario configuration
    scenario_name = os.getenv("SCENARIO", "salehi").lower()
    scenario = ScenarioSettings(
        name=scenario_name,
        transfer_to_operator=(scenario_name == "agrad"),
        audio_src_dir=f"assets/audio/{scenario_name}/src",
    )

    log_level = os.getenv("LOG_LEVEL", "INFO")

    return Settings(
        ari=ari,
        gapgpt=gapgpt,
        vira=vira,
        dialer=dialer,
        operator=operator,
        panel=panel,
        audio=audio,
        concurrency=concurrency,
        timeouts=timeouts,
        sms=sms,
        scenario=scenario,
        log_level=log_level,
    )
