"""Per-beat Gemini TTS synthesis, SHA-256 clip caching, mock synthesis, and voice assembly."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

from cm.models import (
    AudioTimeline,
    Blueprint,
    TimedBeatAudio,
    WordTiming,
)
from cm.paths import atomic_write_text, get_repo_root, run_dir
from cm.state import load_state

DEFAULT_SAMPLE_RATE = 48000
GEMINI_TTS_MODEL_CASCADE: tuple[str, ...] = (
    "gemini-2.5-pro-preview-tts",
    "gemini-3.1-flash-tts-preview",
    "gemini-2.5-flash-preview-tts",
)


def is_fallback_worthy_error(exc: Exception) -> bool:
    """Return True when a Gemini TTS error reflects quota/credits/availability."""
    raw_status = getattr(exc, "code", None)
    if raw_status is None:
        raw_status = getattr(exc, "status_code", None)
    status = int(raw_status) if isinstance(raw_status, int) else None
    msg = str(exc).lower()

    if status == 401 or any(
        tok in msg for tok in ("api_key_invalid", "invalid api key", "unauthenticated")
    ):
        return False

    if status in {402, 403, 404, 429, 503}:
        return True

    return any(
        tok in msg
        for tok in (
            "resource_exhausted",
            "quota",
            "rate limit",
            "credit",
            "billing",
            "overloaded",
            "unavailable",
            "not found",
            "not supported",
            "deprecated",
        )
    )


class TtsProvider(StrEnum):
    """Supported speech synthesis backends."""

    AUTO = "auto"
    GEMINI = "gemini"
    MOCK = "mock"


def load_env(root: Path | None = None) -> None:
    """Load environment variables from .env without overriding existing process vars."""
    env_path = (root or get_repo_root()) / ".env"
    load_dotenv(env_path, override=False)


def compute_cache_key(
    provider: str,
    model: str,
    voice_name: str,
    style_prompt: str | None,
    narration: str,
) -> str:
    """Return a deterministic 16-char SHA-256 cache key for a single beat narration."""
    payload = json.dumps(
        {
            "provider": provider,
            "model": model,
            "voice_name": voice_name,
            "style_prompt": (style_prompt or "").strip(),
            "narration": narration.strip(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def read_wav_duration_sec(path: Path) -> float:
    """Return exact WAV file duration in seconds."""
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def parse_pcm_rate(mime_type: str | None, default: int = 24000) -> int:
    """Extract sample rate from a PCM mime_type like 'audio/L16;codec=pcm;rate=24000'."""
    if not mime_type:
        return default
    match = re.search(r"rate\s*=\s*(\d+)", mime_type, re.IGNORECASE)
    return int(match.group(1)) if match else default


def _detect_audio_extension(raw_bytes: bytes, mime_type: str | None) -> str | None:
    if raw_bytes.startswith(b"RIFF") or (mime_type and "wav" in mime_type.lower()):
        return ".wav"
    if (
        raw_bytes.startswith(b"ID3")
        or raw_bytes.startswith(b"\xff\xfb")
        or (mime_type and ("mp3" in mime_type.lower() or "mpeg" in mime_type.lower()))
    ):
        return ".mp3"
    if raw_bytes.startswith(b"OggS") or (mime_type and "ogg" in mime_type.lower()):
        return ".ogg"
    if raw_bytes.startswith(b"fLaC") or (mime_type and "flac" in mime_type.lower()):
        return ".flac"
    return None


def _run_ffmpeg(args: list[str], error_context: str) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        tail = (proc.stderr or "")[-400:].strip()
        raise RuntimeError(f"{error_context}: {tail}")
    return proc


def normalize_tts_bytes_to_wav(
    raw_bytes: bytes,
    mime_type: str | None,
    out_wav: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> float:
    """Convert raw PCM or encoded TTS bytes into trimmed 48 kHz mono 16-bit WAV."""
    if not raw_bytes:
        raise ValueError("TTS audio payload is empty")

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmpdir_str:
        tmpdir = Path(tmpdir_str)
        ext = _detect_audio_extension(raw_bytes, mime_type)
        if ext is not None:
            raw_in = tmpdir / f"input{ext}"
            raw_in.write_bytes(raw_bytes)
        else:
            pcm_rate = parse_pcm_rate(mime_type, default=24000)
            raw_in = tmpdir / "input.wav"
            with wave.open(str(raw_in), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(pcm_rate)
                wf.writeframes(raw_bytes)

        resampled = tmpdir / "resampled.wav"
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(raw_in),
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(resampled),
            ],
            "FFmpeg TTS resample failed",
        )

        trimmed = tmpdir / "trimmed.wav"
        _run_ffmpeg(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(resampled),
                "-af",
                "silenceremove=start_periods=1:start_threshold=-45dB:start_duration=0.02,"
                "areverse,"
                "silenceremove=start_periods=1:start_threshold=-45dB:start_duration=0.02,"
                "areverse",
                "-ac",
                "1",
                "-ar",
                str(sample_rate),
                "-c:a",
                "pcm_s16le",
                str(trimmed),
            ],
            "FFmpeg TTS trim failed",
        )

        chosen = trimmed if read_wav_duration_sec(trimmed) >= 0.15 else resampled
        tmp_dest: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=out_wav.parent,
                prefix=f".{out_wav.name}.",
                suffix=".tmp.wav",
                delete=False,
            ) as handle:
                tmp_dest = Path(handle.name)
            shutil.copy2(chosen, tmp_dest)
            os.replace(tmp_dest, out_wav)
        except Exception:
            if tmp_dest is not None and tmp_dest.exists():
                tmp_dest.unlink(missing_ok=True)
            raise

    return read_wav_duration_sec(out_wav)


def synthesize_gemini_clip(
    narration: str,
    model: str,
    voice_name: str,
    style_prompt: str | None,
    api_key: str,
    out_wav: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> float:
    """Call Gemini TTS via google-genai SDK and save normalized 48 kHz mono WAV."""
    from google import genai
    from google.genai import types

    prompt = (
        f"Speak in this style: {style_prompt.strip()}\n\n{narration}"
        if style_prompt and style_prompt.strip()
        else narration
    )
    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            response_modalities=["AUDIO"],
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        ),
    )

    parts = getattr(response, "parts", None)
    if not parts:
        candidates = getattr(response, "candidates", None) or []
        if candidates and getattr(candidates[0], "content", None):
            parts = getattr(candidates[0].content, "parts", None)

    for part in parts or []:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline is not None else None
        if data:
            raw_bytes = base64.b64decode(data) if isinstance(data, str) else bytes(data)
            mime_type = getattr(inline, "mime_type", None)
            return normalize_tts_bytes_to_wav(
                raw_bytes=raw_bytes,
                mime_type=mime_type,
                out_wav=out_wav,
                sample_rate=sample_rate,
            )

    raise RuntimeError("Gemini TTS returned no inline audio data")


def synthesize_mock_clip(
    narration: str,
    target_wpm: int,
    out_wav: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> float:
    """Synthesize a deterministic speech-cadence WAV clip matching target_wpm duration."""
    words = [tok for tok in narration.strip().split() if tok]
    word_count = max(1, len(words))
    duration = max(1.0, word_count / (target_wpm / 60.0))
    n_samples = int(round(duration * sample_rate))

    syllable_hz = max(1.5, word_count / duration)
    fade = int(0.015 * sample_rate)
    samples = bytearray()
    for i in range(n_samples):
        t = i / sample_rate
        cadence = 0.45 + 0.55 * abs(math.sin(math.pi * syllable_hz * t))
        edge = 1.0
        if i < fade:
            edge = i / max(1, fade)
        elif n_samples - i < fade:
            edge = (n_samples - i) / max(1, fade)
        carrier = 0.72 * math.sin(2.0 * math.pi * 196.0 * t) + 0.28 * math.sin(
            2.0 * math.pi * 392.0 * t
        )
        val = max(-32767, min(32767, round(carrier * cadence * edge * 0.24 * 32767.0)))
        samples.extend(struct.pack("<h", val))

    out_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=out_wav.parent,
            prefix=f".{out_wav.name}.",
            suffix=".tmp.wav",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
        with wave.open(str(tmp_path), "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(bytes(samples))
        os.replace(tmp_path, out_wav)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return read_wav_duration_sec(out_wav)


def assemble_voice_track(
    beat_clips: list[tuple[Path, int]],
    out_wav: Path,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> list[tuple[float, float, float]]:
    """Concatenate 48 kHz mono PCM clips with sample-exact pause silence."""
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    timings: list[tuple[float, float, float]] = []
    frame_cursor = 0
    tmp_path: Path | None = None

    try:
        with tempfile.NamedTemporaryFile(
            dir=out_wav.parent,
            prefix=f".{out_wav.name}.",
            suffix=".tmp.wav",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)

        with wave.open(str(tmp_path), "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(sample_rate)

            for clip_path, pause_ms in beat_clips:
                with wave.open(str(clip_path), "rb") as in_wf:
                    if (
                        in_wf.getnchannels() != 1
                        or in_wf.getsampwidth() != 2
                        or in_wf.getframerate() != sample_rate
                    ):
                        raise ValueError(
                            f"Clip {clip_path} must be {sample_rate}Hz mono 16-bit PCM"
                        )
                    n_frames = in_wf.getnframes()
                    pcm = in_wf.readframes(n_frames)

                start_frame = frame_cursor
                out_wf.writeframes(pcm)
                frame_cursor += n_frames
                speech_end_frame = frame_cursor

                pause_frames = max(0, round((pause_ms / 1000.0) * sample_rate))
                if pause_frames > 0:
                    out_wf.writeframes(b"\x00\x00" * pause_frames)
                    frame_cursor += pause_frames

                timings.append(
                    (
                        round(start_frame / sample_rate, 4),
                        round(speech_end_frame / sample_rate, 4),
                        round(frame_cursor / sample_rate, 4),
                    )
                )

        os.replace(tmp_path, out_wav)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise

    return timings


def _build_word_timings(
    narration: str, start_sec: float, speech_end_sec: float
) -> list[WordTiming]:
    tokens = [tok for tok in narration.strip().split() if tok]
    if not tokens:
        return []
    span = max(0.2, speech_end_sec - start_sec)
    weights = [max(2, len(re.sub(r"[^a-zA-Z0-9]", "", tok))) for tok in tokens]
    total_w = sum(weights)
    cursor = start_sec
    items: list[WordTiming] = []
    for word, weight in zip(tokens, weights, strict=True):
        share = (weight / total_w) * span
        items.append(
            WordTiming(
                word=word,
                start_sec=round(cursor, 4),
                end_sec=round(cursor + share, 4),
            )
        )
        cursor += share
    return items


def inject_timeline_into_html(html: str, timeline_json: str) -> str:
    """Embed or update <script id='cm-timeline-data'> inside a scene HTML document."""
    safe_json = timeline_json.replace("</", "<\\/")
    tag = f'<script id="cm-timeline-data" type="application/json">\n{safe_json}\n  </script>'
    existing = re.compile(
        r'<script\s+id="cm-timeline-data"[^>]*>.*?</script>',
        re.DOTALL,
    )
    if existing.search(html):
        return existing.sub(lambda _: tag, html, count=1)

    marker = re.search(r'<script\s+src="[^"]*runtime\.js"', html)
    if marker:
        idx = marker.start()
        return f"{html[:idx]}{tag}\n\n  {html[idx:]}"

    return html.replace("</body>", f"  {tag}\n</body>", 1)


def sync_timeline_into_scene(
    slug: str,
    timeline: AudioTimeline,
    root: Path | None = None,
) -> bool:
    """Sync runs/<slug>/audio/timeline.json into runs/<slug>/scene.html if present."""
    scene_path = run_dir(slug, root) / "scene.html"
    if not scene_path.is_file():
        return False
    html = scene_path.read_text(encoding="utf-8")
    updated = inject_timeline_into_html(html, timeline.model_dump_json(indent=2))
    atomic_write_text(scene_path, updated)
    return True


def synthesize_run_tts(
    slug: str,
    provider: TtsProvider = TtsProvider.AUTO,
    model: str | None = None,
    voice_name: str | None = None,
    force: bool = False,
    root: Path | None = None,
) -> AudioTimeline:
    """Synthesize per-beat TTS with SHA-256 caching and assemble voice_master.wav."""
    load_env(root)
    load_state(slug, root)
    rdir = run_dir(slug, root)
    bp_path = rdir / "blueprint.json"
    if not bp_path.is_file():
        raise FileNotFoundError(f"Blueprint not found for run '{slug}': {bp_path}")

    blueprint = Blueprint.model_validate_json(bp_path.read_text(encoding="utf-8"))

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if provider == TtsProvider.MOCK:
        resolved_provider = TtsProvider.MOCK
    else:
        if not api_key:
            raise ValueError(
                "GEMINI_API_KEY is not set in .env "
                "(add your key to .env or pass --provider mock for offline testing)"
            )
        resolved_provider = TtsProvider.GEMINI

    resolved_voice = voice_name or blueprint.voice.voice_name
    style_prompt = blueprint.voice.style_prompt
    model_cascade: tuple[str, ...] = (
        ("mock-tts-v1",)
        if resolved_provider == TtsProvider.MOCK
        else ((model,) if model else GEMINI_TTS_MODEL_CASCADE)
    )

    audio_dir = rdir / "audio"
    cache_dir = audio_dir / "cache"
    beats_dir = audio_dir / "beats"
    cache_dir.mkdir(parents=True, exist_ok=True)
    beats_dir.mkdir(parents=True, exist_ok=True)

    active_idx = 0
    fallback_events: list[str] = []
    beat_clip_specs: list[tuple[Path, int]] = []
    cache_keys: list[str] = []
    beat_rel_paths: list[str] = []
    beat_models: list[str] = []

    for beat in blueprint.beats:
        chosen_key = ""
        chosen_model = ""
        chosen_wav = cache_dir / "unset.wav"

        while active_idx < len(model_cascade):
            current_model = model_cascade[active_idx]
            key = compute_cache_key(
                provider=resolved_provider.value,
                model=current_model,
                voice_name=resolved_voice,
                style_prompt=style_prompt,
                narration=beat.narration,
            )
            cached_wav = cache_dir / f"{key}.wav"
            if force or not cached_wav.is_file():
                if resolved_provider == TtsProvider.MOCK:
                    synthesize_mock_clip(
                        narration=beat.narration,
                        target_wpm=blueprint.voice.target_wpm,
                        out_wav=cached_wav,
                    )
                else:
                    try:
                        synthesize_gemini_clip(
                            narration=beat.narration,
                            model=current_model,
                            voice_name=resolved_voice,
                            style_prompt=style_prompt,
                            api_key=api_key,
                            out_wav=cached_wav,
                        )
                    except Exception as exc:
                        if not is_fallback_worthy_error(exc):
                            raise
                        if active_idx + 1 < len(model_cascade):
                            next_model = model_cascade[active_idx + 1]
                            fallback_events.append(
                                f"Beat {beat.id}: downgraded {current_model} -> {next_model} ({exc})"
                            )
                            active_idx += 1
                            continue
                        raise RuntimeError(
                            f"All Gemini TTS models exhausted for beat '{beat.id}' "
                            f"({', '.join(model_cascade)}): {exc}"
                        ) from exc

            chosen_key = key
            chosen_model = current_model
            chosen_wav = cached_wav
            break

        beat_wav = beats_dir / f"{beat.id}.wav"
        shutil.copy2(chosen_wav, beat_wav)
        beat_clip_specs.append((beat_wav, beat.pause_after_ms))
        cache_keys.append(chosen_key)
        beat_rel_paths.append(f"beats/{beat.id}.wav")
        beat_models.append(chosen_model)

    voice_master_wav = audio_dir / "voice_master.wav"
    boundaries = assemble_voice_track(beat_clip_specs, voice_master_wav)

    timed_beats: list[TimedBeatAudio] = []
    for beat, key, beat_model, rel_path, (start_sec, speech_end_sec, end_sec) in zip(
        blueprint.beats, cache_keys, beat_models, beat_rel_paths, boundaries, strict=True
    ):
        timed_beats.append(
            TimedBeatAudio(
                beat_id=beat.id,
                kind=beat.kind,
                version_label=beat.version_label,
                narration=beat.narration,
                cache_key=key,
                clip_path=rel_path,
                provider=resolved_provider.value,  # type: ignore[arg-type]
                model=beat_model,
                start_sec=start_sec,
                speech_end_sec=speech_end_sec,
                end_sec=end_sec,
                duration_sec=round(end_sec - start_sec, 4),
                pause_after_ms=beat.pause_after_ms,
                word_timings=_build_word_timings(beat.narration, start_sec, speech_end_sec),
            )
        )

    total_duration = timed_beats[-1].end_sec if timed_beats else 0.0
    timeline = AudioTimeline(
        slug=slug,
        provider=resolved_provider.value,  # type: ignore[arg-type]
        model=model_cascade[active_idx],
        voice_name=resolved_voice,
        sample_rate=DEFAULT_SAMPLE_RATE,
        total_duration_sec=round(total_duration, 4),
        voice_track_path="voice_master.wav",
        fallback_events=fallback_events,
        beats=timed_beats,
    )

    timeline_path = audio_dir / "timeline.json"
    atomic_write_text(timeline_path, timeline.model_dump_json(indent=2) + "\n")
    sync_timeline_into_scene(slug, timeline, root=root)
    return timeline
