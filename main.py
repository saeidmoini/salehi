import logging
import time

from config import get_settings
from core.ari_client import AriClient
from core.ari_ws import AriWebSocketClient
from llm.client import GapGPTClient
from logic.dialer import Dialer
from logic.marketing_outreach import MarketingScenario
from sessions.session_manager import SessionManager
from utils.audio_sync import ensure_audio_assets


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    logger = logging.getLogger("app")

    # Ensure audio assets are converted and available to Asterisk.
    ensure_audio_assets(settings.audio)

    ari_client = AriClient(settings.ari)
    llm_client = GapGPTClient(settings.gapgpt)
    session_manager = SessionManager(ari_client, None)  # placeholder to allow scenario access
    scenario = MarketingScenario(settings, ari_client, llm_client, session_manager)
    session_manager.scenario_handler = scenario
    dialer = Dialer(settings, ari_client, session_manager)
    scenario.attach_dialer(dialer)

    ws_client = AriWebSocketClient(settings.ari, session_manager.handle_event)

    logger.info("Starting ARI WebSocket listener and dialer")
    ws_client.start()
    dialer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        dialer.stop()
        ws_client.stop()


if __name__ == "__main__":
    main()
