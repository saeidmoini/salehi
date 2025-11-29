import logging
import os
import shutil
import subprocess
from pathlib import Path

from config.settings import AudioSettings


logger = logging.getLogger(__name__)


def ensure_audio_assets(settings: AudioSettings) -> None:
    """
    Convert any mp3 files under src_dir to 16k mono WAV under wav_dir
    and copy wav files into the Asterisk sounds directory (and language
    subdir if present).
    """
    src_dir = Path(settings.src_dir)
    wav_dir = Path(settings.wav_dir)
    ast_dir = Path(settings.ast_sound_dir)

    wav_dir.mkdir(parents=True, exist_ok=True)
    ast_dir.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found; skipping audio conversion")
    else:
        for mp3_path in src_dir.glob("*.mp3"):
            wav_path = wav_dir / f"{mp3_path.stem}.wav"
            _convert_mp3_to_wav(mp3_path, wav_path)

    _copy_wavs_to_asterisk(wav_dir, ast_dir)


def _convert_mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    if wav_path.exists() and wav_path.stat().st_mtime >= mp3_path.stat().st_mtime:
        return
    logger.info("Converting %s -> %s", mp3_path, wav_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.warning("ffmpeg conversion failed for %s: %s", mp3_path, exc)


def _copy_wavs_to_asterisk(wav_dir: Path, ast_dir: Path) -> None:
    targets = [ast_dir]
    # If the target is .../custom and an /en/custom sibling exists or is creatable, sync there too.
    if ast_dir.name == "custom":
        lang_dir = ast_dir.parent / "en" / "custom"
        targets.append(lang_dir)

    for wav_path in wav_dir.glob("*.wav"):
        for target_dir in targets:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / wav_path.name
            try:
                shutil.copy2(wav_path, target)
                os.chmod(target, 0o644)
                logger.info("Synced prompt %s to %s", wav_path.name, target)
            except Exception as exc:
                logger.warning("Failed to copy %s to %s: %s", wav_path, target, exc)
