"""FFmpeg SFX mixing, background music sidechain ducking, and -14 LUFS mastering."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from cm.models import (
    AudioTimeline,
    Blueprint,
    PipelineStage,
    ScheduledSfx,
    SfxTrigger,
    StageStatus,
    TimedBeatAudio,
)
from cm.paths import atomic_write_text, run_dir
from cm.sfx import init_sfx_pack, music_dir, resolve_sfx_file
from cm.state import load_state, update_stage
from cm.tts import sync_timeline_into_scene


def resolve_trigger_time(trigger: SfxTrigger, beat: TimedBeatAudio) -> float:
    """Resolve a beat-relative SFX trigger ('start'|'mid'|'end'|float) to seconds."""
    speech_span = max(0.05, beat.speech_end_sec - beat.start_sec)
    if trigger.at == "start":
        return round(beat.start_sec, 4)
    if trigger.at == "mid":
        return round(beat.start_sec + speech_span * 0.5, 4)
    if trigger.at == "end":
        return round(max(beat.start_sec, beat.speech_end_sec - 0.15), 4)
    return round(beat.start_sec + float(trigger.at) * speech_span, 4)


def measure_integrated_lufs(audio_path: Path) -> float:
    """Measure integrated LUFS loudness of an audio file via FFmpeg loudnorm."""
    proc = subprocess.run(
        [
            "ffmpeg",
            "-i",
            str(audio_path),
            "-af",
            "loudnorm=I=-14:TP=-1.5:LRA=11:print_format=json",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"FFmpeg loudnorm measurement failed: {(proc.stderr or '')[-400:]}")

    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", proc.stderr, re.DOTALL)
    if not matches:
        raise RuntimeError("Could not parse loudnorm JSON telemetry from FFmpeg output")
    data = json.loads(matches[-1])
    return round(float(data["input_i"]), 2)


def _find_music_bed(music_path: Path | None, root: Path | None) -> Path | None:
    if music_path is not None:
        if not music_path.is_file():
            raise FileNotFoundError(f"Background music file not found: {music_path}")
        return music_path
    mdir = music_dir(root)
    for name in ("bed_default.wav", "bed_default.mp3", "bed_default.ogg"):
        candidate = mdir / name
        if candidate.is_file():
            return candidate
    return None


def mix_run_audio(
    slug: str,
    music_path: Path | None = None,
    music_gain_db: float = -22.0,
    target_lufs: float = -14.0,
    auto_init_sfx: bool = True,
    root: Path | None = None,
) -> AudioTimeline:
    """Mix voiceover, beat-aligned SFX cues, and ducked music bed to -14 LUFS."""
    state = load_state(slug, root)
    rdir = run_dir(slug, root)
    audio_dir = rdir / "audio"
    bp_path = rdir / "blueprint.json"
    timeline_path = audio_dir / "timeline.json"

    if not timeline_path.is_file():
        raise FileNotFoundError(
            f"Audio timeline missing for '{slug}': {timeline_path} (run: cm tts {slug})"
        )

    blueprint = Blueprint.model_validate_json(bp_path.read_text(encoding="utf-8"))
    timeline = AudioTimeline.model_validate_json(timeline_path.read_text(encoding="utf-8"))
    voice_wav = audio_dir / timeline.voice_track_path
    if not voice_wav.is_file():
        raise FileNotFoundError(f"Voice track missing: {voice_wav}")

    if auto_init_sfx:
        init_sfx_pack(force=False, root=root)

    beat_map = {b.beat_id: b for b in timeline.beats}
    cue_counts: dict[str, int] = {}
    scheduled: list[ScheduledSfx] = []

    for bp_beat in blueprint.beats:
        timed_beat = beat_map.get(bp_beat.id)
        if timed_beat is None:
            continue
        for trig in bp_beat.sfx:
            occ = cue_counts.get(trig.cue.value, 0)
            cue_counts[trig.cue.value] = occ + 1
            sfx_file = resolve_sfx_file(trig.cue, occurrence_index=occ, root=root)
            scheduled.append(
                ScheduledSfx(
                    beat_id=bp_beat.id,
                    cue=trig.cue,
                    time_sec=resolve_trigger_time(trig, timed_beat),
                    gain_db=trig.gain_db,
                    file_path=str(sfx_file),
                )
            )

    bed_path = _find_music_bed(music_path, root)

    cmd: list[str] = ["ffmpeg", "-y", "-i", str(voice_wav)]
    filter_parts: list[str] = [
        "[0:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo[voice_base]"
    ]
    mix_labels: list[str] = []
    next_idx = 1

    if bed_path is not None:
        cmd.extend(["-stream_loop", "-1", "-i", str(bed_path)])
        dur = max(0.5, timeline.total_duration_sec)
        fade_start = max(0.0, dur - 0.35)
        filter_parts.append("[voice_base]asplit=2[v_mix][v_sc]")
        filter_parts.append(
            f"[{next_idx}:a]atrim=0:{dur:.4f},asetpts=PTS-STARTPTS,"
            f"aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={music_gain_db:.2f}dB,"
            f"afade=t=out:st={fade_start:.4f}:d=0.35[m_pre]"
        )
        filter_parts.append(
            "[m_pre][v_sc]sidechaincompress=threshold=0.02:ratio=6:attack=20:release=250[music_ducked]"
        )
        mix_labels.extend(["[v_mix]", "[music_ducked]"])
        next_idx += 1
    else:
        mix_labels.append("[voice_base]")

    for idx, event in enumerate(scheduled):
        cmd.extend(["-i", event.file_path])
        delay_ms = max(0, round(event.time_sec * 1000))
        label = f"sfx_{idx}"
        filter_parts.append(
            f"[{next_idx}:a]aformat=sample_fmts=fltp:sample_rates=48000:channel_layouts=stereo,"
            f"volume={event.gain_db:.2f}dB,adelay={delay_ms}|{delay_ms}[{label}]"
        )
        mix_labels.append(f"[{label}]")
        next_idx += 1

    if len(mix_labels) == 1:
        filter_parts.append(
            f"{mix_labels[0]}loudnorm=I={target_lufs:.2f}:TP=-1.5:LRA=11,"
            "aresample=48000[out]"
        )
    else:
        joined = "".join(mix_labels)
        filter_parts.append(
            f"{joined}amix=inputs={len(mix_labels)}:duration=first:normalize=0,"
            f"loudnorm=I={target_lufs:.2f}:TP=-1.5:LRA=11,aresample=48000[out]"
        )

    master_wav = audio_dir / "master.wav"
    tmp_master: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=audio_dir,
            prefix=".master.",
            suffix=".tmp.wav",
            delete=False,
        ) as handle:
            tmp_master = Path(handle.name)

        cmd.extend(
            [
                "-filter_complex",
                ";".join(filter_parts),
                "-map",
                "[out]",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(tmp_master),
            ]
        )
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(f"FFmpeg audio mix failed: {(proc.stderr or '')[-500:]}")
        os.replace(tmp_master, master_wav)
    except Exception:
        if tmp_master is not None and tmp_master.exists():
            tmp_master.unlink(missing_ok=True)
        raise

    measured_lufs = measure_integrated_lufs(master_wav)
    timeline.master_audio_path = "master.wav"
    timeline.target_lufs = target_lufs
    timeline.measured_lufs = measured_lufs
    timeline.sfx_events = scheduled

    atomic_write_text(timeline_path, timeline.model_dump_json(indent=2) + "\n")
    sync_timeline_into_scene(slug, timeline, root=root)

    advance = state.current_stage in (
        PipelineStage.IDEATE,
        PipelineStage.BLUEPRINT,
        PipelineStage.AUDIO,
    )
    update_stage(
        slug=slug,
        stage=PipelineStage.AUDIO,
        status=StageStatus.PASSED,
        notes=(
            f"{timeline.provider} TTS ({len(timeline.beats)} beats, "
            f"{len(scheduled)} SFX, {measured_lufs:.1f} LUFS)"
        ),
        advance_on_pass=advance,
        root=root,
    )
    return timeline
