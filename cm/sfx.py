"""Semantic SFX cue registry and procedural 48 kHz WAV synthesizer."""

from __future__ import annotations

import json
import math
import os
import random
import struct
import tempfile
import wave
from pathlib import Path

from cm.models import SfxCue
from cm.paths import assets_dir, atomic_write_text

SFX_SAMPLE_RATE = 48000


def sfx_dir(root: Path | None = None) -> Path:
    """Return the assets/sfx directory."""
    return assets_dir(root) / "sfx"


def sfx_manifest_path(root: Path | None = None) -> Path:
    """Return the path to assets/sfx/manifest.json."""
    return sfx_dir(root) / "manifest.json"


def music_dir(root: Path | None = None) -> Path:
    """Return the assets/music directory."""
    return assets_dir(root) / "music"


def default_manifest() -> dict[str, list[str]]:
    """Return default 1:1 cue-to-WAV mappings for all semantic SFX slots."""
    return {cue.value: [f"{cue.value}.wav"] for cue in SfxCue}


def load_sfx_manifest(root: Path | None = None) -> dict[str, list[str]]:
    """Load assets/sfx/manifest.json, falling back to defaults if missing."""
    path = sfx_manifest_path(root)
    if not path.is_file():
        return default_manifest()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError(f"Corrupt SFX manifest at {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise ValueError(f"SFX manifest at {path} must be a JSON object")

    manifest: dict[str, list[str]] = {}
    for key, files in raw.items():
        if not isinstance(key, str) or not isinstance(files, list) or not files:
            raise ValueError(f"Invalid manifest entry for '{key}' in {path}")
        manifest[key] = [str(item) for item in files]
    return manifest


def _read_wav_duration_sec(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return round(wf.getnframes() / float(wf.getframerate()), 3)


def _write_mono_wav(
    path: Path, samples: list[float], sample_rate: int = SFX_SAMPLE_RATE
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    peak = max((abs(s) for s in samples), default=0.0)
    scale = (0.82 / peak) if peak > 0.0 else 1.0
    pcm_bytes = b"".join(
        struct.pack("<h", max(-32767, min(32767, round(s * scale * 32767.0))))
        for s in samples
    )

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp.wav",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm_bytes)
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def _envelope(t: float, dur: float, attack: float = 0.004, release: float = 0.03) -> float:
    if t < attack:
        return t / max(1e-6, attack)
    rem = dur - t
    if rem < release:
        return max(0.0, rem / max(1e-6, release))
    return 1.0


def _synth_sweep(
    dur: float,
    f0: float,
    f1: float,
    decay: float = 1.0,
    noise_mix: float = 0.0,
    seed: int = 1,
    harmonics: float = 0.2,
) -> list[float]:
    n = int(dur * SFX_SAMPLE_RATE)
    rng = random.Random(seed)
    phase = 0.0
    out: list[float] = []
    for i in range(n):
        t = i / SFX_SAMPLE_RATE
        prog = t / dur
        freq = f0 + (f1 - f0) * prog
        phase += 2.0 * math.pi * freq / SFX_SAMPLE_RATE
        tone = math.sin(phase) + harmonics * math.sin(2.0 * phase)
        env = _envelope(t, dur) * math.exp(-decay * prog)
        sample = tone * (1.0 - noise_mix) + (rng.uniform(-1.0, 1.0) * noise_mix)
        out.append(sample * env)
    return out


def _synth_steps(
    dur: float,
    freqs: list[float],
    decay: float = 2.0,
    square_mix: float = 0.0,
) -> list[float]:
    n = int(dur * SFX_SAMPLE_RATE)
    step_len = max(1, n // len(freqs))
    phase = 0.0
    out: list[float] = []
    for i in range(n):
        t = i / SFX_SAMPLE_RATE
        idx = min(len(freqs) - 1, i // step_len)
        freq = freqs[idx]
        phase += 2.0 * math.pi * freq / SFX_SAMPLE_RATE
        sine = math.sin(phase)
        sq = 1.0 if sine >= 0.0 else -1.0
        tone = sine * (1.0 - square_mix) + sq * square_mix
        env = _envelope(t, dur, attack=0.002, release=0.025) * math.exp(-decay * (t / dur))
        out.append(tone * env)
    return out


def _synth_whoosh(dur: float = 0.28, seed: int = 7) -> list[float]:
    n = int(dur * SFX_SAMPLE_RATE)
    rng = random.Random(seed)
    lp = 0.0
    out: list[float] = []
    for i in range(n):
        t = i / SFX_SAMPLE_RATE
        prog = t / dur
        alpha = 0.04 + 0.22 * math.sin(math.pi * prog)
        lp = lp + alpha * (rng.uniform(-1.0, 1.0) - lp)
        env = math.sin(math.pi * prog) ** 1.4
        out.append(lp * env)
    return out


def synthesize_cue_samples(cue: SfxCue) -> list[float]:
    """Generate clean 48 kHz mono float samples for any semantic SFX cue."""
    if cue == SfxCue.POP_BUILD:
        return _synth_sweep(0.12, 760.0, 220.0, decay=4.5)
    if cue == SfxCue.CLICK_UI:
        return _synth_sweep(0.06, 1900.0, 950.0, decay=6.0, noise_mix=0.2, seed=11)
    if cue == SfxCue.WHOOSH:
        return _synth_whoosh(0.28, seed=17)
    if cue == SfxCue.TICK_DATA:
        return _synth_steps(0.09, [1480.0, 2200.0], decay=4.0)
    if cue == SfxCue.RISER_STRESS:
        return _synth_sweep(0.55, 180.0, 960.0, decay=0.2, noise_mix=0.12, seed=23)
    if cue == SfxCue.PULSE_LOAD:
        return _synth_steps(0.30, [165.0, 220.0, 165.0], decay=1.4, square_mix=0.2)
    if cue == SfxCue.IMPACT_FAIL:
        return _synth_sweep(0.42, 150.0, 36.0, decay=4.0, noise_mix=0.35, seed=31, harmonics=0.45)
    if cue == SfxCue.GLITCH_ERROR:
        rng = random.Random(1337)
        return _synth_steps(
            0.24,
            [rng.choice([320.0, 640.0, 980.0, 1420.0, 210.0]) for _ in range(8)],
            decay=2.2,
            square_mix=0.55,
        )
    if cue == SfxCue.ALARM_BREACH:
        return _synth_steps(0.36, [880.0, 622.0, 880.0, 622.0], decay=0.8, square_mix=0.25)
    if cue == SfxCue.STAMP_HIT:
        return _synth_sweep(0.26, 220.0, 52.0, decay=5.5, noise_mix=0.30, seed=41, harmonics=0.35)
    if cue == SfxCue.CHIME_FIX:
        return _synth_steps(0.34, [523.25, 659.25, 783.99], decay=2.2)
    # SfxCue.LOCK_UPGRADE
    return _synth_steps(0.28, [440.0, 880.0], decay=2.6)


def init_sfx_pack(force: bool = False, root: Path | None = None) -> dict[str, object]:
    """Initialize assets/sfx/manifest.json and procedural starter WAV files."""
    sdir = sfx_dir(root)
    mdir = music_dir(root)
    sdir.mkdir(parents=True, exist_ok=True)
    mdir.mkdir(parents=True, exist_ok=True)

    mpath = sfx_manifest_path(root)
    if force or not mpath.is_file():
        atomic_write_text(mpath, json.dumps(default_manifest(), indent=2) + "\n")

    manifest = load_sfx_manifest(root)
    generated: list[str] = []
    preserved: list[str] = []

    for cue in SfxCue:
        variants = manifest.get(cue.value) or [f"{cue.value}.wav"]
        target_wav = sdir / variants[0]
        if target_wav.is_file() and not force:
            preserved.append(target_wav.name)
            continue
        _write_mono_wav(target_wav, synthesize_cue_samples(cue))
        generated.append(target_wav.name)

    return {
        "sfx_dir": str(sdir),
        "manifest_path": str(mpath),
        "generated": generated,
        "preserved": preserved,
    }


def resolve_sfx_file(
    cue: SfxCue,
    occurrence_index: int = 0,
    root: Path | None = None,
) -> Path:
    """Resolve a semantic SfxCue to an existing WAV file via assets/sfx/manifest.json."""
    manifest = load_sfx_manifest(root)
    variants = manifest.get(cue.value)
    if not variants:
        raise FileNotFoundError(f"No SFX variants registered for cue '{cue.value}'")
    chosen = variants[occurrence_index % len(variants)]
    path = sfx_dir(root) / chosen
    if not path.is_file():
        raise FileNotFoundError(
            f"Missing audio file for cue '{cue.value}': {path} (run: cm sfx init)"
        )
    return path


def list_sfx_slots(root: Path | None = None) -> list[dict[str, object]]:
    """Inspect all semantic SFX cue slots and their backing WAV files."""
    manifest = load_sfx_manifest(root)
    sdir = sfx_dir(root)
    slots: list[dict[str, object]] = []
    for cue in SfxCue:
        files = manifest.get(cue.value, [])
        existing = [sdir / fname for fname in files if (sdir / fname).is_file()]
        dur = _read_wav_duration_sec(existing[0]) if existing else None
        slots.append(
            {
                "cue": cue.value,
                "files": files,
                "exists": len(existing) == len(files) and len(files) > 0,
                "duration_sec": dur,
            }
        )
    return slots
