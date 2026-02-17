import asyncio
import logging
import signal
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import get_settings
from core.ari_client import AriClient
from core.ari_ws import AriWebSocketClient
from llm.client import GapGPTClient
from logic.dialer import Dialer
from logic.flow_engine import FlowEngine
from logic.scenario_registry import ScenarioRegistry
from integrations.panel.client import PanelClient
from sessions.session_manager import SessionManager
from stt_tts.vira_stt import ViraSTTClient
from stt_tts.vira_tts import ViraTTSClient
from utils.audio_sync import ensure_audio_assets

ALLOWED_LOG_PREFIXES = (
    "app",
    "core",
    "logic",
    "sessions",
    "stt_tts",
    "llm",
    "utils",
    "config",
    "main",
    "integrations",
)


def _build_handler(formatter: logging.Formatter, log_path: Path | None = None) -> logging.Handler:
    handler: logging.Handler
    if log_path:
        handler = RotatingFileHandler(log_path, maxBytes=5 * 1024 * 1024, backupCount=5)
    else:
        handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    handler.addFilter(lambda record: record.name.startswith(ALLOWED_LOG_PREFIXES))
    return handler


def configure_logging(level: str) -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)

    formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(log_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    handler_console = _build_handler(formatter)
    handler_file = _build_handler(formatter, log_dir / "app.log")

    root.addHandler(handler_console)
    root.addHandler(handler_file)


async def async_main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("app")

    # Ensure audio assets are converted and available to Asterisk without blocking the loop.
    await asyncio.to_thread(ensure_audio_assets, settings.audio)

    stt_semaphore = asyncio.Semaphore(settings.concurrency.max_parallel_stt)
    tts_semaphore = asyncio.Semaphore(settings.concurrency.max_parallel_tts)
    llm_semaphore = asyncio.Semaphore(settings.concurrency.max_parallel_llm)

    ari_client = AriClient(
        settings.ari,
        timeout=settings.timeouts.ari_timeout,
        max_connections=settings.concurrency.http_max_connections,
    )
    stt_client = ViraSTTClient(
        settings.vira,
        timeout=settings.timeouts.stt_timeout,
        max_connections=settings.concurrency.http_max_connections,
        semaphore=stt_semaphore,
    )
    tts_client = ViraTTSClient(
        settings.vira,
        timeout=settings.timeouts.tts_timeout,
        max_connections=settings.concurrency.http_max_connections,
        semaphore=tts_semaphore,
    )
    llm_client = GapGPTClient(
        settings.gapgpt,
        timeout=settings.timeouts.llm_timeout,
        max_connections=settings.concurrency.http_max_connections,
        semaphore=llm_semaphore,
    )
    panel_client: PanelClient | None = None
    if settings.panel.base_url and settings.panel.api_token:
        panel_client = PanelClient(
            base_url=settings.panel.base_url,
            api_token=settings.panel.api_token,
            company=settings.company,
            timeout=settings.timeouts.http_timeout,
            max_connections=settings.concurrency.http_max_connections,
            default_retry=settings.dialer.default_retry,
        )
    # Initialize multi-scenario architecture
    logger.info("Loading scenarios from %s", settings.scenarios_dir)
    scenario_registry = ScenarioRegistry(scenarios_dir=settings.scenarios_dir)
    logger.info("Loaded %d scenarios: %s", len(scenario_registry.get_names()), scenario_registry.get_names())

    # Create SessionManager with scenario registry
    session_manager = SessionManager(
        ari_client,
        scenario_handler=None,  # Will be set below
        scenario_registry=scenario_registry,
        allowed_inbound_numbers=settings.dialer.outbound_numbers,
    )

    # Initialize FlowEngine with all clients
    flow_engine = FlowEngine(
        settings=settings,
        ari_client=ari_client,
        llm_client=llm_client,
        stt_client=stt_client,
        session_manager=session_manager,
        registry=scenario_registry,
        panel_client=panel_client,
    )
    session_manager.scenario_handler = flow_engine

    # Initialize Dialer with scenario registry
    dialer = Dialer(
        settings,
        ari_client,
        session_manager,
        scenario_registry=scenario_registry,
        panel_client=panel_client,
    )
    session_manager.attach_dialer(dialer)
    flow_engine.attach_dialer(dialer)

    # Register available scenarios with panel
    if panel_client:
        scenario_names = scenario_registry.get_names()
        if scenario_names:
            await panel_client.register_scenarios(scenario_names)
            logger.info("Registered %d scenarios with panel", len(scenario_names))

    ws_client = AriWebSocketClient(settings.ari, session_manager.handle_event)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # Signals not available on some platforms (e.g., Windows).
            pass

    logger.info("Starting ARI WebSocket listener and dialer")
    tasks = [
        asyncio.create_task(ws_client.run()),
        asyncio.create_task(dialer.run(stop_event)),
    ]
    try:
        await stop_event.wait()
    finally:
        await ws_client.stop()
        await dialer.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            ari_client.close(),
            stt_client.close(),
            tts_client.close(),
            llm_client.close(),
            panel_client.close() if panel_client else asyncio.sleep(0),
            return_exceptions=True,
        )
        logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(async_main())
