"""Real headless Chromium (Playwright) tests for the 2D Dark-Blueprint Visual Engine."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

from cm.models import FormatPreset, TopicDomain
from cm.scene import create_preview_server, scaffold_scene
from cm.state import init_run

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_BP = REPO_ROOT / "examples" / "password_system.blueprint.json"
EXAMPLE_SCENE = REPO_ROOT / "examples" / "password_system.scene.html"


@pytest.fixture(scope="session")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)
        yield b
        b.close()


def _attach_error_collectors(page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
    )
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    return console_errors, page_errors


def test_math_and_timeline_determinism_in_chromium(browser) -> None:
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    console_errors, page_errors = _attach_error_collectors(page)
    page.goto(EXAMPLE_SCENE.resolve().as_uri(), wait_until="load")

    results = page.evaluate(
        """() => {
            const m = window.CM.math;
            const rng1 = m.seededRandom(42);
            const rng2 = m.seededRandom(42);
            const seq1 = [rng1(), rng1(), rng1()];
            const seq2 = [rng2(), rng2(), rng2()];
            return {
                clampLow: m.clamp(-5, 0, 10),
                clampHigh: m.clamp(25, 0, 10),
                lerpMid: m.lerp(10, 20, 0.5),
                remapVal: m.remap(50, 0, 100, 200, 400),
                easeCubic: m.easeOutCubic(1),
                easeInOut: m.easeInOutCubic(0.5),
                easeExpo: m.easeOutExpo(1),
                easeBack: m.easeOutBack(1),
                springZero: m.spring(0),
                springSettled: m.spring(2.0),
                springOverdamped: m.spring(0.5, { stiffness: 100, damping: 30 }),
                shakeOutOfBounds: m.shake(-0.1),
                shakeExpired: m.shake(0.5),
                shakeActive: m.shake(0.1),
                seq1,
                seq2,
            };
        }"""
    )

    assert results["clampLow"] == 0
    assert results["clampHigh"] == 10
    assert results["lerpMid"] == 15
    assert results["remapVal"] == 300
    assert results["easeCubic"] == 1
    assert results["easeInOut"] == pytest.approx(0.5)
    assert results["easeExpo"] == 1
    assert results["easeBack"] == pytest.approx(1.0)
    assert results["springZero"] == 0
    assert results["springSettled"] == 1
    assert 0.5 < results["springOverdamped"] <= 1.0
    assert results["shakeOutOfBounds"] == {"x": 0, "y": 0, "rot": 0}
    assert results["shakeExpired"] == {"x": 0, "y": 0, "rot": 0}
    assert results["shakeActive"]["x"] != 0
    assert results["seq1"] == results["seq2"]
    assert console_errors == []
    assert page_errors == []
    page.close()


def test_example_password_system_scene_27_keyframes_and_seek_determinism(browser) -> None:
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    console_errors, page_errors = _attach_error_collectors(page)
    page.goto(EXAMPLE_SCENE.resolve().as_uri(), wait_until="load")

    keyframes = page.evaluate("() => window.__HARNESS__.getKeyframes()")
    assert len(keyframes) == 27  # 9 beats x (start, mid, end)

    mid_svgs: dict[str, str] = {}
    for kf in keyframes:
        seek_res = page.evaluate("(t) => window.__HARNESS__.seek(t)", kf["t"])
        assert seek_res["beatId"] == kf["beatId"]
        audit = page.evaluate("() => window.__HARNESS__.auditLayout()")
        assert audit["passed"] is True, f"Audit failed at {kf}: {audit['violations']}"
        assert audit["stageElementCount"] > 0
        if kf["label"] == "mid":
            mid_svgs[kf["beatId"]] = page.evaluate(
                "() => document.getElementById('cm-stage-svg').innerHTML"
            )

    # Every consecutive beat visually mutates the persistent diagram
    beat_ids = list(mid_svgs.keys())
    for prev_id, next_id in zip(beat_ids[:-1], beat_ids[1:], strict=True):
        assert mid_svgs[prev_id] != mid_svgs[next_id], (
            f"Beats {prev_id} and {next_id} produced identical SVG markup"
        )

    # Seek determinism: clamp <0, >totalDuration, jump forward/back, exact same SVG
    total = page.evaluate("() => window.__HARNESS__.getState().totalDuration")
    s_neg = page.evaluate("() => window.__HARNESS__.seek(-5)")
    assert s_neg["t"] == 0
    s_over = page.evaluate("(t) => window.__HARNESS__.seek(t)", total + 100)
    assert s_over["t"] == pytest.approx(total)

    mid_t = keyframes[10]["t"]
    page.evaluate("(t) => window.__HARNESS__.seek(t)", mid_t)
    snap_a = page.evaluate("() => document.getElementById('cm-stage-svg').innerHTML")
    page.evaluate("() => window.__HARNESS__.seek(0)")
    page.evaluate("(t) => window.__HARNESS__.seek(t)", mid_t)
    snap_b = page.evaluate("() => document.getElementById('cm-stage-svg').innerHTML")
    assert snap_a == snap_b

    assert console_errors == []
    assert page_errors == []
    page.close()


def test_auditor_catches_injected_layout_and_runtime_faults(browser) -> None:
    page = browser.new_page(viewport={"width": 1080, "height": 1920})
    page.goto(EXAMPLE_SCENE.resolve().as_uri(), wait_until="load")

    # Baseline passes
    assert page.evaluate("() => window.__HARNESS__.auditLayout().passed") is True

    # 1. Inject small text -> FONT_TOO_SMALL
    page.evaluate(
        """() => {
            const p = document.createElement('p');
            p.id = 'bad-small';
            p.style.fontSize = '14px';
            p.textContent = 'Tiny unreadable text';
            document.getElementById('cm-safe-frame').appendChild(p);
        }"""
    )
    rep_small = page.evaluate("() => window.__HARNESS__.auditLayout()")
    assert "FONT_TOO_SMALL" in {v["code"] for v in rep_small["violations"]}
    page.evaluate("() => document.getElementById('bad-small').remove()")

    # 2. Inject SVG shape outside safe zone -> SAFE_ZONE_OVERFLOW
    page.evaluate(
        """() => {
            const svg = document.getElementById('cm-stage-svg');
            const rect = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
            rect.setAttribute('x', '-120');
            rect.setAttribute('y', '0');
            rect.setAttribute('width', '80');
            rect.setAttribute('height', '80');
            rect.setAttribute('fill', '#ef4444');
            svg.appendChild(rect);
        }"""
    )
    rep_overflow = page.evaluate("() => window.__HARNESS__.auditLayout()")
    assert "SAFE_ZONE_OVERFLOW" in {v["code"] for v in rep_overflow["violations"]}

    # 3. Inject clipped HTML box -> TEXT_CLIPPED
    page.evaluate(
        """() => {
            const box = document.createElement('div');
            box.id = 'bad-clip';
            box.style.width = '40px';
            box.style.height = '34px';
            box.style.overflow = 'hidden';
            box.style.whiteSpace = 'nowrap';
            box.style.fontSize = '30px';
            box.textContent = 'Overflowing unbreakable label string';
            document.getElementById('cm-safe-frame').appendChild(box);
        }"""
    )
    rep_clip = page.evaluate("() => window.__HARNESS__.auditLayout()")
    assert "TEXT_CLIPPED" in {v["code"] for v in rep_clip["violations"]}
    page.evaluate("() => document.getElementById('bad-clip').remove()")

    # 4. Empty SVG stage -> EMPTY_STAGE
    page.evaluate("() => { document.getElementById('cm-stage-svg').innerHTML = ''; }")
    rep_empty = page.evaluate("() => window.__HARNESS__.auditLayout()")
    assert "EMPTY_STAGE" in {v["code"] for v in rep_empty["violations"]}
    page.close()


def test_scaffolded_short_and_long_scenes_pass_chromium_audit(
    browser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CM_ROOT", str(tmp_path))

    # Short 9:16
    init_run("short-test", from_blueprint=EXAMPLE_BP)
    short_scene = scaffold_scene("short-test")
    page_short = browser.new_page(viewport={"width": 1080, "height": 1920})
    errs_s, page_errs_s = _attach_error_collectors(page_short)
    page_short.goto(short_scene.resolve().as_uri(), wait_until="load")
    for kf in page_short.evaluate("() => window.__HARNESS__.getKeyframes()")[::3]:
        page_short.evaluate("(t) => window.__HARNESS__.seek(t)", kf["t"])
        audit = page_short.evaluate("() => window.__HARNESS__.auditLayout()")
        assert audit["passed"] is True, audit["violations"]
    assert errs_s == [] and page_errs_s == []
    page_short.close()

    # Long 16:9
    raw_long = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
    raw_long["format"] = FormatPreset.LONG_16X9.value
    long_bp = tmp_path / "long_bp.json"
    long_bp.write_text(json.dumps(raw_long), encoding="utf-8")
    init_run("long-test", from_blueprint=long_bp, topic_domain=TopicDomain.CS)
    long_scene = scaffold_scene("long-test")

    page_long = browser.new_page(viewport={"width": 1920, "height": 1080})
    errs_l, page_errs_l = _attach_error_collectors(page_long)
    page_long.goto(long_scene.resolve().as_uri(), wait_until="load")
    for kf in page_long.evaluate("() => window.__HARNESS__.getKeyframes()")[::3]:
        page_long.evaluate("(t) => window.__HARNESS__.seek(t)", kf["t"])
        audit = page_long.evaluate("() => window.__HARNESS__.auditLayout()")
        assert audit["viewport"] == {"width": 1920, "height": 1080}
        assert audit["passed"] is True, audit["violations"]
    assert errs_l == [] and page_errs_l == []
    page_long.close()


def test_audio_timeline_override_and_interactive_preview_mode(browser) -> None:
    # Serve via local HTTP server with ?preview=1
    server = create_preview_server(REPO_ROOT, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        errs, page_errs = _attach_error_collectors(page)
        url = f"http://127.0.0.1:{port}/examples/password_system.scene.html?preview=1"
        page.goto(url, wait_until="load")

        # Interactive bar mounted ONLY in ?preview=1
        assert page.locator("#cm-preview-bar").is_visible()
        assert page.locator("#cm-audit-badge").text_content() == "AUDIT PASS"

        # Navigate Next / Prev beat and toggle Safe-Zone overlay
        page.click("#cm-btn-next")
        assert page.evaluate("() => window.__HARNESS__.getState().beatIndex") == 1
        page.click("#cm-btn-prev")
        assert page.evaluate("() => window.__HARNESS__.getState().beatIndex") == 0

        assert page.locator("#cm-safe-overlay").is_visible() is False
        page.click("#cm-btn-safe")
        assert page.locator("#cm-safe-overlay").is_visible() is True

        # Audit stays accurate even while scaled in desktop preview
        assert page.evaluate("() => window.__HARNESS__.auditLayout().passed") is True

        # Re-initialize with custom audioTimeline override & exact word timings
        bp_data = json.loads(EXAMPLE_BP.read_text(encoding="utf-8"))
        custom_audio = {
            "beats": [
                {
                    "beat_id": bp_data["beats"][0]["id"],
                    "start_sec": 0.0,
                    "end_sec": 2.0,
                    "speech_end_sec": 1.8,
                    "word_timings": [
                        {"word": "CUSTOM", "start_sec": 0.0, "end_sec": 0.9},
                        {"word": "HOOK", "start_sec": 0.9, "end_sec": 1.8},
                    ],
                }
            ]
        }
        page.evaluate(
            """([bp, atl]) => {
                window.__HARNESS__.init({ blueprint: bp, audioTimeline: atl, preview: false });
                window.__HARNESS__.seek(1.1);
            }""",
            [bp_data, custom_audio],
        )
        active_word = page.locator(".cm-caption-word.is-active").text_content()
        assert active_word == "HOOK"
        assert page.evaluate("() => window.__HARNESS__.getState().beats[0].endSec") == 2.0
        assert errs == [] and page_errs == []
        page.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
