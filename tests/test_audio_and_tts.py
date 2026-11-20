"""Exhaustive tests for procedural SFX, Gemini/mock TTS caching, and FFmpeg mastering."""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from cm.audio import measure_integrated_lufs, mix_run_audio, resolve_trigger_time
from cm.cli import app
from cm.models import (
    AudioTimeline,
    BeatKind,
    PipelineStage,
    SfxCue,
    SfxTrigger,
    StageStatus,
    TimedBeatAudio,
)
from cm.scene import scaffold_scene
from cm.sfx import (
    SFX_SAMPLE_RATE,
    init_sfx_pack,
    list_sfx_slots,
    load_sfx_manifest,
    resolve_sfx_file,
    sfx_dir,
)
from cm.state import init_run, load_state, update_stage
from cm.tts import (
    TtsProvider,
    assemble_voice_track,
    compute_cache_key,
    normalize_tts_bytes_to_wav,
    parse_pcm_rate,
    read_wav_duration_sec,
    sync_timeline_into_scene,
    synthesize_gemini_clip,
    synthesize_mock_clip,
    synthesize_run_tts,
)

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"


def _tone_pcm_bytes(duration_sec: float = 0.6, rate: int = 24000, freq: float = 260.0) -> bytes:
    n = int(duration_sec * rate)
    buf = bytearray()
    for i in range(n):
        val = round(math.sin(2.0 * math.pi * freq * (i / rate)) * 0.35 * 32767.0)
        buf.extend(struct.pack("<h", val))
    return bytes(buf)


def _tone_wav_bytes(duration_sec: float = 0.6, rate: int = 24000) -> bytes:
    pcm = _tone_pcm_bytes(duration_sec=duration_sec, rate=rate)
    mem = io.BytesIO()
    with wave.open(mem, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(pcm)
    return mem.getvalue()


def test_sfx_init_preserve_variant_rotation_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Default manifest loads before init
    assert len(load_sfx_manifest()) == len(SfxCue)

    summary = init_sfx_pack()
    assert len(summary["generated"]) == len(SfxCue)
    assert summary["preserved"] == []

    # Verify all 12 generated WAVs are valid 48 kHz mono and non-silent
    slots = list_sfx_slots()
    assert len(slots) == len(SfxCue)
    for slot in slots:
        assert slot["exists"] is True
        assert slot["duration_sec"] > 0.04
        wav_path = sfx_dir() / slot["files"][0]
        with wave.open(str(wav_path), "rb") as wf:
            assert (wf.getnchannels(), wf.getsampwidth(), wf.getframerate()) == (1, 2, SFX_SAMPLE_RATE)

    # Custom WAV is preserved when force=False, overwritten when force=True
    custom_pop = sfx_dir() / "pop_build.wav"
    custom_pop.write_bytes(_tone_wav_bytes(0.15, rate=SFX_SAMPLE_RATE))
    res_preserve = init_sfx_pack(force=False)
    assert "pop_build.wav" in res_preserve["preserved"]
    assert read_wav_duration_sec(custom_pop) == pytest.approx(0.15, abs=0.01)

    res_force = init_sfx_pack(force=True)
    assert len(res_force["generated"]) == len(SfxCue)

    # Variant rotation via custom manifest
    (sfx_dir() / "pop_build_alt.wav").write_bytes(_tone_wav_bytes(0.18, rate=SFX_SAMPLE_RATE))
    manifest_path = sfx_dir() / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pop_build"] = ["pop_build.wav", "pop_build_alt.wav"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert resolve_sfx_file(SfxCue.POP_BUILD, 0).name == "pop_build.wav"
    assert resolve_sfx_file(SfxCue.POP_BUILD, 1).name == "pop_build_alt.wav"
    assert resolve_sfx_file(SfxCue.POP_BUILD, 2).name == "pop_build.wav"

    # Missing variant / missing file / corrupt manifest errors
    manifest["pop_build"] = ["missing_file.wav"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="Missing audio file"):
        resolve_sfx_file(SfxCue.POP_BUILD)

    manifest_path.write_text("{corrupt", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt SFX manifest"):
        load_sfx_manifest()

    manifest_path.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a JSON object"):
        load_sfx_manifest()

    manifest_path.write_text('{"pop_build": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid manifest entry"):
        load_sfx_manifest()


def test_tts_helpers_normalization_and_mocked_gemini(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    k1 = compute_cache_key("gemini", "m1", "Charon", "Crisp", "Hello world")
    k2 = compute_cache_key("gemini", "m1", "Charon", "Crisp", "Hello world")
    k3 = compute_cache_key("gemini", "m1", "Puck", "Crisp", "Hello world")
    assert k1 == k2 and k1 != k3 and len(k1) == 16

    assert parse_pcm_rate(None) == 24000
    assert parse_pcm_rate("audio/L16;codec=pcm;rate=16000") == 16000

    with pytest.raises(ValueError, match="empty"):
        normalize_tts_bytes_to_wav(b"", None, tmp_path / "empty.wav")

    # Raw PCM 24kHz -> normalized 48kHz WAV
    pcm_out = tmp_path / "from_pcm.wav"
    dur_pcm = normalize_tts_bytes_to_wav(
        _tone_pcm_bytes(0.55, rate=24000),
        "audio/L16;codec=pcm;rate=24000",
        pcm_out,
    )
    assert dur_pcm == pytest.approx(0.55, abs=0.05)
    with wave.open(str(pcm_out), "rb") as wf:
        assert (wf.getnchannels(), wf.getframerate()) == (1, 48000)

    # Short silent-ish clip falls back cleanly to untrimmed resampled WAV (< 0.15s after trim)
    short_out = tmp_path / "short_fallback.wav"
    dur_short = normalize_tts_bytes_to_wav(
        b"\x00\x00" * 4800,
        "audio/L16;codec=pcm;rate=24000",
        short_out,
    )
    assert dur_short == pytest.approx(0.2, abs=0.02)

    # Mock google.genai.Client returning base64 RIFF WAV via candidates fallback
    wav_b64 = base64.b64encode(_tone_wav_bytes(0.5, rate=24000)).decode("ascii")
    calls: list[dict] = []

    class FakeClient:
        def __init__(self, api_key: str) -> None:
            self.api_key = api_key
            self.models = self

        def generate_content(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                parts=None,
                candidates=[
                    SimpleNamespace(
                        content=SimpleNamespace(
                            parts=[
                                SimpleNamespace(
                                    inline_data=SimpleNamespace(
                                        data=wav_b64,
                                        mime_type="audio/wav",
                                    )
                                )
                            ]
                        )
                    )
                ],
            )

    monkeypatch.setattr("google.genai.Client", FakeClient)
    gem_out = tmp_path / "gemini_clip.wav"
    gem_dur = synthesize_gemini_clip(
        narration="Test narration",
        model="gemini-2.5-flash-preview-tts",
        voice_name="Charon",
        style_prompt="Fast and clear",
        api_key="test-key",
        out_wav=gem_out,
    )
    assert gem_dur == pytest.approx(0.5, abs=0.05)
    assert "Speak in this style: Fast and clear" in calls[0]["contents"]

    # Empty Gemini response raises clear RuntimeError
    class EmptyClient:
        def __init__(self, api_key: str) -> None:
            self.models = self

        def generate_content(self, **kwargs):
            _ = kwargs
            return SimpleNamespace(parts=[SimpleNamespace(inline_data=None)], candidates=[])

    monkeypatch.setattr("google.genai.Client", EmptyClient)
    with pytest.raises(RuntimeError, match="no inline audio data"):
        synthesize_gemini_clip("Hi", "m", "Charon", None, "key", tmp_path / "fail.wav")


def test_tts_cache_hit_invalidation_env_and_scene_sync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    init_run("tts-cache", from_blueprint=EXAMPLE_BP)

    # Missing GEMINI_API_KEY fails clearly in auto/gemini mode
    with pytest.raises(ValueError, match="GEMINI_API_KEY is not set"):
        synthesize_run_tts("tts-cache", provider=TtsProvider.AUTO)

    # Mock synthesis + SHA-256 caching behavior
    synth_calls: list[str] = []
    orig_mock = synthesize_mock_clip

    def spy_mock(narration: str, target_wpm: int, out_wav: Path, sample_rate: int = 48000) -> float:
        synth_calls.append(narration)
        return orig_mock(narration, target_wpm, out_wav, sample_rate=sample_rate)

    monkeypatch.setattr("cm.tts.synthesize_mock_clip", spy_mock)

    tl1 = synthesize_run_tts("tts-cache", provider=TtsProvider.MOCK)
    assert len(synth_calls) == 9
    assert len(tl1.beats) == 9
    assert tl1.total_duration_sec > 35.0
    assert (tmp_path / "runs" / "tts-cache" / "audio" / "voice_master.wav").is_file()

    # Re-run without changes -> 0 new synthesis calls (100% cache hit)
    synth_calls.clear()
    synthesize_run_tts("tts-cache", provider=TtsProvider.MOCK)
    assert len(synth_calls) == 0

    # Edit 1 beat narration -> only 1 beat re-synthesized
    bp_path = tmp_path / "runs" / "tts-cache" / "blueprint.json"
    bp_data = json.loads(bp_path.read_text(encoding="utf-8"))
    bp_data["beats"][0]["narration"] = "Updated hook narration line here!"
    bp_path.write_text(json.dumps(bp_data, indent=2), encoding="utf-8")

    synthesize_run_tts("tts-cache", provider=TtsProvider.MOCK)
    assert synth_calls == ["Updated hook narration line here!"]

    # force=True re-synthesizes all 9 beats
    synth_calls.clear()
    tl_forced = synthesize_run_tts("tts-cache", provider=TtsProvider.MOCK, force=True)
    assert len(synth_calls) == 9

    # Fresh scaffold_scene auto-embeds existing timeline.json AND subsequent sync updates it
    scene_path = scaffold_scene("tts-cache")
    assert 'id="cm-timeline-data"' in scene_path.read_text(encoding="utf-8")
    assert sync_timeline_into_scene("tts-cache", tl_forced) is True
    assert scene_path.read_text(encoding="utf-8").count('id="cm-timeline-data"') == 1

    # .env loading picks up GEMINI_API_KEY in AUTO mode
    (tmp_path / ".env").write_text("GEMINI_API_KEY=from-dotenv-file\n", encoding="utf-8")
    pcm_payload = _tone_pcm_bytes(0.45, rate=24000)

    class AutoFakeClient:
        def __init__(self, api_key: str) -> None:
            assert api_key == "from-dotenv-file"
            self.models = self

        def generate_content(self, **kwargs):
            _ = kwargs
            return SimpleNamespace(
                parts=[
                    SimpleNamespace(
                        inline_data=SimpleNamespace(
                            data=pcm_payload,
                            mime_type="audio/L16;codec=pcm;rate=24000",
                        )
                    )
                ]
            )

    monkeypatch.setattr("google.genai.Client", AutoFakeClient)
    tl_auto = synthesize_run_tts("tts-cache", provider=TtsProvider.AUTO)
    assert tl_auto.provider == "gemini"


def test_assemble_voice_track_rejects_mismatched_wav_format(tmp_path: Path) -> None:
    bad_wav = tmp_path / "bad_rate.wav"
    bad_wav.write_bytes(_tone_wav_bytes(0.2, rate=24000))
    with pytest.raises(ValueError, match="48000Hz mono 16-bit PCM"):
        assemble_voice_track([(bad_wav, 100)], tmp_path / "out.wav")


def test_audio_mix_lufs_sfx_music_ducking_and_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Missing timeline error
    init_run("mix-demo", from_blueprint=EXAMPLE_BP)
    with pytest.raises(FileNotFoundError, match="Audio timeline missing"):
        mix_run_audio("mix-demo")

    # Resolve trigger times across all 4 offset types
    sample_beat = TimedBeatAudio(
        beat_id="b1",
        kind=BeatKind.FAIL,
        version_label="v1.0",
        narration="Boom",
        cache_key="abc",
        clip_path="beats/b1.wav",
        provider="mock",
        start_sec=10.0,
        speech_end_sec=14.0,
        end_sec=14.2,
        duration_sec=4.2,
        pause_after_ms=200,
    )
    assert resolve_trigger_time(SfxTrigger(cue=SfxCue.IMPACT_FAIL, at="start"), sample_beat) == 10.0
    assert resolve_trigger_time(SfxTrigger(cue=SfxCue.IMPACT_FAIL, at="mid"), sample_beat) == 12.0
    assert resolve_trigger_time(SfxTrigger(cue=SfxCue.IMPACT_FAIL, at="end"), sample_beat) == 13.85
    assert resolve_trigger_time(SfxTrigger(cue=SfxCue.IMPACT_FAIL, at=0.25), sample_beat) == 11.0

    # End-to-end mock TTS + real FFmpeg SFX mix (with explicit 'end' cue too)
    bp_path = tmp_path / "runs" / "mix-demo" / "blueprint.json"
    bp_raw = json.loads(bp_path.read_text(encoding="utf-8"))
    bp_raw["beats"][0]["sfx"].append({"cue": "whoosh", "at": "end", "gain_db": -12.0})
    bp_path.write_text(json.dumps(bp_raw, indent=2), encoding="utf-8")

    synthesize_run_tts("mix-demo", provider=TtsProvider.MOCK)
    mixed = mix_run_audio("mix-demo")

    master_wav = tmp_path / "runs" / "mix-demo" / "audio" / "master.wav"
    assert master_wav.is_file()
    with wave.open(str(master_wav), "rb") as wf:
        assert (wf.getnchannels(), wf.getframerate()) == (2, 48000)
        master_dur = wf.getnframes() / float(wf.getframerate())
    assert master_dur == pytest.approx(mixed.total_duration_sec, abs=0.3)
    assert mixed.measured_lufs == pytest.approx(-14.0, abs=1.5)
    assert measure_integrated_lufs(master_wav) == pytest.approx(mixed.measured_lufs, abs=0.1)
    assert len(mixed.sfx_events) >= 13

    state = load_state("mix-demo")
    assert state.stages[PipelineStage.AUDIO].status == StageStatus.PASSED
    assert state.current_stage == PipelineStage.SCENE

    # Music bed sidechain ducking test (via auto bed_default.wav + explicit --music)
    bed_wav = tmp_path / "assets" / "music" / "bed_default.wav"
    bed_wav.parent.mkdir(parents=True, exist_ok=True)
    bed_wav.write_bytes(_tone_wav_bytes(3.0, rate=48000))
    # Advance state to QA first to verify audio mix does not regress current_stage
    update_stage("mix-demo", PipelineStage.SCENE, StageStatus.PASSED)
    mixed_music = mix_run_audio("mix-demo")
    assert mixed_music.measured_lufs == pytest.approx(-14.0, abs=1.5)
    assert load_state("mix-demo").current_stage == PipelineStage.QA

    with pytest.raises(FileNotFoundError, match="music file not found"):
        mix_run_audio("mix-demo", music_path=tmp_path / "missing_bed.wav")

    # Voice-only blueprint (0 SFX, no music bed) mixes cleanly
    bed_wav.unlink()
    for b in bp_raw["beats"]:
        b["sfx"] = []
    bp_path.write_text(json.dumps(bp_raw, indent=2), encoding="utf-8")
    mixed_voice_only = mix_run_audio("mix-demo")
    assert mixed_voice_only.sfx_events == []
    assert mixed_voice_only.measured_lufs == pytest.approx(-14.0, abs=1.5)


def test_cli_sfx_tts_audio_and_schema_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    # cm sfx list before init -> MISSING
    res_list_empty = runner.invoke(app, ["sfx", "list"])
    assert res_list_empty.exit_code == 0
    assert "MISSING" in res_list_empty.output
    res_list_empty.output.encode("cp1252")

    # cm sfx init (human + --json)
    res_sfx_init = runner.invoke(app, ["sfx", "init"])
    assert res_sfx_init.exit_code == 0, res_sfx_init.output
    res_sfx_init.output.encode("cp1252")

    res_sfx_json = runner.invoke(app, ["sfx", "list", "--json"])
    assert res_sfx_json.exit_code == 0
    assert all(slot["exists"] for slot in json.loads(res_sfx_json.output)["slots"])

    # cm tts --provider mock --no-mix followed by cm audio mix
    assert runner.invoke(app, ["new", "cli-audio", "--from-blueprint", str(EXAMPLE_BP)]).exit_code == 0

    res_nomix = runner.invoke(app, ["tts", "cli-audio", "--provider", "mock", "--no-mix"])
    assert res_nomix.exit_code == 0, res_nomix.output
    assert "voice only" in res_nomix.output
    res_nomix.output.encode("cp1252")
    assert load_state("cli-audio").stages[PipelineStage.AUDIO].status == StageStatus.IN_PROGRESS

    res_mix = runner.invoke(app, ["audio", "mix", "cli-audio"])
    assert res_mix.exit_code == 0, res_mix.output
    assert "Mastered cli-audio/audio/master.wav" in res_mix.output
    res_mix.output.encode("cp1252")
    assert load_state("cli-audio").stages[PipelineStage.AUDIO].status == StageStatus.PASSED

    # Full one-shot cm tts --provider mock --json
    res_tts_json = runner.invoke(app, ["tts", "cli-audio", "--provider", "mock", "--json"])
    assert res_tts_json.exit_code == 0, res_tts_json.output
    tl_obj = AudioTimeline.model_validate_json(res_tts_json.output)
    assert tl_obj.measured_lufs is not None

    # Error paths in CLI
    assert runner.invoke(app, ["tts", "cli-audio", "--provider", "auto", "--json"]).exit_code == 1
    assert runner.invoke(app, ["audio", "mix", "missing-slug"]).exit_code == 1

    # Schema timeline export
    res_schema = runner.invoke(app, ["schema", "timeline"])
    assert res_schema.exit_code == 0
    assert "sfx_events" in json.loads(res_schema.output)["properties"]
