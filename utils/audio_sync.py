import logging
import os
import shutil
import subprocess
from pathlib import Path

from config.settings import AudioSettings


logger = logging.getLogger(__name__)


def ensure_audio_assets(settings: AudioSettings, audio_src_dir: str = None) -> None:
    """
    Convert any mp3 files under src_dir to 16k mono WAV under wav_dir
    and copy wav files into the Asterisk sounds directory (and language
    subdir if present).

    Args:
        settings: Audio settings (wav_dir, ast_sound_dir)
        audio_src_dir: Scenario-specific audio source directory (overrides settings.src_dir)
    """
    src_dir = Path(audio_src_dir) if audio_src_dir else Path(settings.src_dir)
    wav_dir = Path(settings.wav_dir)
    ast_dir = Path(settings.ast_sound_dir)

    wav_dir.mkdir(parents=True, exist_ok=True)

    if not shutil.which("ffmpeg"):
        logger.warning("ffmpeg not found; skipping audio conversion")
    else:
        for mp3_path in src_dir.glob("*.mp3"):
            wav_path = wav_dir / f"{mp3_path.stem}.wav"
            _convert_mp3_to_wav(mp3_path, wav_path)
            _convert_mp3_to_ulaw(mp3_path, wav_dir / f"{mp3_path.stem}.ulaw")
            _convert_mp3_to_alaw(mp3_path, wav_dir / f"{mp3_path.stem}.alaw")

    try:
        _copy_wavs_to_asterisk(wav_dir, ast_dir)
    except PermissionError:
        logger.warning(
            "Permission denied copying audio into %s. "
            "Run with sufficient privileges or set AST_SOUND_DIR to a writable path.",
            ast_dir,
        )


def _convert_mp3_to_wav(mp3_path: Path, wav_path: Path) -> None:
    logger.info("Converting %s -> %s", mp3_path, wav_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-ac",
        "1",
        "-c:a",
        "pcm_s16le",
        "-ar",
        "8000",
        str(wav_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.warning("ffmpeg conversion failed for %s: %s", mp3_path, exc)


def _convert_mp3_to_ulaw(mp3_path: Path, ulaw_path: Path) -> None:
    logger.info("Converting %s -> %s", mp3_path, ulaw_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "mulaw",
        str(ulaw_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.warning("ffmpeg ulaw conversion failed for %s: %s", mp3_path, exc)


def _convert_mp3_to_alaw(mp3_path: Path, alaw_path: Path) -> None:
    logger.info("Converting %s -> %s", mp3_path, alaw_path)
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(mp3_path),
        "-ac",
        "1",
        "-ar",
        "8000",
        "-f",
        "alaw",
        str(alaw_path),
    ]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        logger.warning("ffmpeg alaw conversion failed for %s: %s", mp3_path, exc)


def _copy_wavs_to_asterisk(wav_dir: Path, ast_dir: Path) -> None:
    targets = _build_target_dirs(ast_dir)

    for pattern in ("*.wav", "*.ulaw", "*.alaw"):
        for wav_path in wav_dir.glob(pattern):
            for target_dir in targets:
                try:
                    target_dir.mkdir(parents=True, exist_ok=True)
                    target = target_dir / wav_path.name
                    shutil.copy2(wav_path, target)
                    os.chmod(target, 0o644)
                    logger.info("Synced prompt %s to %s", wav_path.name, target)
                except PermissionError:
                    logger.warning(
                        "Permission denied copying %s to %s. "
                        "Run with sufficient privileges or adjust AST_SOUND_DIR.",
                        wav_path,
                        target_dir,
                    )
                except Exception as exc:
                    logger.warning("Failed to copy %s to %s: %s", wav_path, target_dir, exc)


def _build_target_dirs(ast_dir: Path) -> set[Path]:
    """
    Build a set of target directories:
    - Always includes ast_dir
    - If ast_dir is language-specific (e.g., .../en/custom), also include base .../custom
    - If ast_dir is base .../custom, also include .../en/custom
    """
    targets: set[Path] = {ast_dir}
    if ast_dir.name != "custom":
        return targets

    parent = ast_dir.parent
    # If parent looks like a language code (length 2 or 'en'), add base custom and en/custom
    if len(parent.name) == 2 or parent.name.lower() == "en":
        base_custom = parent.parent / "custom"
        targets.add(base_custom)
        en_custom = parent.parent / "en" / "custom"
        targets.add(en_custom)
    else:
        en_custom = parent / "en" / "custom"
        targets.add(en_custom)

    return targets
