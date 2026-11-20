"""2D scene scaffolding, preview timeline builder, and local preview HTTP server."""

from __future__ import annotations

import json
import os
import re
import shutil
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from pydantic import ValidationError

from cm.models import (
    Beat,
    Blueprint,
    PipelineStage,
    StageStatus,
)
from cm.paths import (
    atomic_write_text,
    get_repo_root,
    resolve_engine_dir,
    run_dir,
)
from cm.state import load_state, update_stage
from cm.tts import inject_timeline_into_html


def _build_word_timings(
    narration: str, start_sec: float, speech_end_sec: float
) -> list[dict[str, float | str]]:
    tokens = [tok for tok in narration.strip().split() if tok]
    if not tokens:
        return []
    span = max(0.2, speech_end_sec - start_sec)
    weights = [max(2, len(re.sub(r"[^a-zA-Z0-9]", "", tok))) for tok in tokens]
    total_w = sum(weights)
    cursor = start_sec
    timings: list[dict[str, float | str]] = []
    for word, weight in zip(tokens, weights, strict=True):
        share = (weight / total_w) * span
        timings.append(
            {
                "word": word,
                "start_sec": round(cursor, 4),
                "end_sec": round(cursor + share, 4),
            }
        )
        cursor += share
    return timings


def _estimate_beat_span(beat: Beat, target_wpm: int) -> tuple[float, float]:
    pause_sec = beat.pause_after_ms / 1000.0
    if beat.estimated_duration_sec is not None:
        total = float(beat.estimated_duration_sec)
        speech_span = max(0.2, total - min(pause_sec, total * 0.4))
        return total, speech_span

    word_count = len(beat.narration.split())
    speech_span = max(1.0, word_count / (target_wpm / 60.0))
    return speech_span + pause_sec, speech_span


def build_preview_timeline(blueprint: Blueprint) -> dict[str, object]:
    """Build deterministic beat and word timings matching runtime.js defaults."""
    wpm = blueprint.voice.target_wpm
    cursor = 0.0
    version_progression: list[str] = []
    beats_out: list[dict[str, object]] = []

    for beat in blueprint.beats:
        if not version_progression or version_progression[-1] != beat.version_label:
            version_progression.append(beat.version_label)

        total_sec, speech_span = _estimate_beat_span(beat, wpm)
        start_sec = cursor
        end_sec = start_sec + total_sec
        speech_end_sec = min(end_sec, start_sec + speech_span)
        cursor = end_sec

        beats_out.append(
            {
                "beat_id": beat.id,
                "kind": beat.kind.value,
                "version_label": beat.version_label,
                "start_sec": round(start_sec, 4),
                "end_sec": round(end_sec, 4),
                "speech_end_sec": round(speech_end_sec, 4),
                "duration_sec": round(total_sec, 4),
                "words": _build_word_timings(beat.narration, start_sec, speech_end_sec),
            }
        )

    return {
        "slug": blueprint.slug,
        "total_duration_sec": round(cursor, 4),
        "version_progression": version_progression,
        "beats": beats_out,
    }


def render_starter_scene_html(blueprint: Blueprint, engine_base_url: str) -> str:
    """Render an audit-passing starter scene.html for a validated Blueprint."""
    safe_base = engine_base_url.rstrip("/")
    bp_json = blueprint.model_dump_json(indent=2).replace("</", "<\\/")
    safe_title = (
        blueprint.title.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>{safe_title} - Content Machine 3.0</title>
  <link rel="stylesheet" href="{safe_base}/tokens.css" />
</head>
<body>
  <div id="cm-root"></div>

  <script id="cm-blueprint-data" type="application/json">
{bp_json}
  </script>

  <script src="{safe_base}/runtime.js"></script>
  <script src="{safe_base}/primitives.js"></script>
  <script>
    (function () {{
      const P = window.CM.primitives;

      function stageAccent(kind) {{
        if (kind === "fail") return "crimson";
        if (kind === "stress_test") return "amber";
        if (kind === "upgrade" || kind === "payoff") return "emerald";
        return "cyan";
      }}

      function renderStage(ctx) {{
        const svg = ctx.svg;
        const beat = ctx.beat;
        const t = ctx.t;
        const p = ctx.beatProgress;
        const ease = ctx.CM.math.easeOutCubic(Math.min(1, p * 2.2));
        const acc = stageAccent(beat.kind);
        const isWide = ctx.spec.preset === "long_16x9";

        P.clear(svg);

        if (isWide) {{
          if (beat.kind === "hook") {{
            P.node(svg, {{
              x: 640, y: 120, w: 480, h: 150,
              title: "Baseline Concept", subtitle: ctx.blueprint.topic_domain.toUpperCase() + " v0.1",
              icon: "unlock", accent: "violet", scale: 0.95 + 0.05 * ease,
            }});
            P.gauge(svg, {{
              x: 610, y: 380, w: 540,
              label: "STAGE", valueText: "HOOK", ratio: 0.08, accent: "violet",
            }});
            return;
          }}
          if (beat.kind === "payoff") {{
            P.shieldAura(svg, {{ x: 40, y: 30, w: 1680, h: 490, t: t, progress: ease, accent: "emerald" }});
            P.codeCard(svg, {{
              x: 420, y: 80, w: 920,
              title: "Final Architecture (" + beat.version_label + ")",
              lines: ["1. All failure modes solved", "2. System status: RESILIENT", "3. Progression: " + ctx.timeline.versionProgression.join(" -> ")],
              highlightLine: 1,
              accent: "emerald",
            }});
            P.gauge(svg, {{
              x: 610, y: 400, w: 540,
              label: "FINAL STATUS", valueText: "RESILIENT", ratio: 1.0, accent: "emerald",
            }});
            return;
          }}
          if (beat.kind === "upgrade") {{
            P.shieldAura(svg, {{ x: 40, y: 30, w: 1680, h: 360, t: t, progress: ease, accent: "emerald" }});
          }}
          P.node(svg, {{
            x: 80, y: 120, w: 360, h: 140,
            title: "Client Input", subtitle: ctx.blueprint.topic_domain.toUpperCase(),
            icon: "user", accent: "cyan", scale: 0.95 + 0.05 * ease,
          }});
          P.pipe(svg, {{
            from: {{ x: 446, y: 190 }}, to: {{ x: 676, y: 190 }},
            accent: acc, progress: ease,
          }});
          P.packetFlow(svg, {{
            from: {{ x: 446, y: 190 }}, to: {{ x: 676, y: 190 }},
            t: t, count: 3, speed: beat.kind === "fail" ? 1.5 : 0.75, accent: acc,
          }});
          P.node(svg, {{
            x: 682, y: 120, w: 400, h: 140,
            title: "Core Engine", subtitle: beat.system_status,
            icon: beat.kind === "fail" ? "warning" : "server",
            accent: acc, badge: beat.version_label,
          }});
          P.pipe(svg, {{
            from: {{ x: 1088, y: 190 }}, to: {{ x: 1318, y: 190 }},
            accent: acc, progress: ease,
          }});
          P.node(svg, {{
            x: 1324, y: 120, w: 356, h: 140,
            title: "State Store", subtitle: beat.kind.toUpperCase(),
            icon: "database", accent: acc,
          }});
          P.gauge(svg, {{
            x: 610, y: 420, w: 540,
            label: "STAGE",
            valueText: (ctx.beatIndex + 1) + " / " + ctx.beatCount,
            ratio: (ctx.beatIndex + 1) / Math.max(1, ctx.beatCount),
            accent: acc,
          }});
          if (beat.kind === "fail" && beat.failure_stamp) {{
            P.failureStamp(svg, {{ text: beat.failure_stamp, x: 880, y: 310, progress: Math.min(1, p * 2.4) }});
          }}
          return;
        }}

        if (beat.kind === "hook") {{
          P.node(svg, {{
            x: 198, y: 240, w: 460, h: 150,
            title: "Baseline Concept", subtitle: ctx.blueprint.topic_domain.toUpperCase() + " v0.1",
            icon: "unlock", accent: "violet", scale: 0.94 + 0.06 * ease,
          }});
          P.gauge(svg, {{
            x: 178, y: 694, w: 500,
            label: "PROGRESS", valueText: "HOOK", ratio: 0.08, accent: "violet",
          }});
          return;
        }}

        if (beat.kind === "payoff") {{
          P.shieldAura(svg, {{ x: 44, y: 44, w: 768, h: 600, t: t, progress: ease, accent: "emerald" }});
          P.codeCard(svg, {{
            x: 88, y: 180, w: 680,
            title: "Final System (" + beat.version_label + ")",
            lines: ["1. Failure modes resolved", "2. Status: RESILIENT", "3. Path: " + ctx.timeline.versionProgression.join("->")],
            highlightLine: 1,
            accent: "emerald",
          }});
          P.gauge(svg, {{
            x: 178, y: 694, w: 500,
            label: "FINAL STATUS", valueText: "RESILIENT", ratio: 1.0, accent: "emerald",
          }});
          return;
        }}

        if (beat.kind === "upgrade") {{
          P.shieldAura(svg, {{ x: 44, y: 36, w: 768, h: 616, t: t, progress: ease, accent: "emerald" }});
        }}
        P.node(svg, {{
          x: 208, y: 58, w: 440, h: 124,
          title: "Client Request", subtitle: ctx.blueprint.topic_domain.toUpperCase() + " input",
          icon: "user", accent: "cyan", scale: 0.94 + 0.06 * ease,
        }});
        P.pipe(svg, {{
          from: {{ x: 428, y: 188 }}, to: {{ x: 428, y: 278 }},
          accent: acc, progress: ease,
        }});
        P.packetFlow(svg, {{
          from: {{ x: 428, y: 188 }}, to: {{ x: 428, y: 472 }},
          t: t, count: 3, speed: beat.kind === "fail" ? 1.4 : 0.75, accent: acc,
        }});
        P.node(svg, {{
          x: 188, y: 286, w: 480, h: 128,
          title: "System Core", subtitle: "status: " + beat.system_status,
          icon: beat.kind === "fail" ? "attacker" : "server",
          accent: acc, badge: beat.version_label,
        }});
        P.pipe(svg, {{
          from: {{ x: 428, y: 420 }}, to: {{ x: 428, y: 492 }},
          accent: acc, progress: ease,
        }});
        P.node(svg, {{
          x: 198, y: 500, w: 460, h: 124,
          title: "Data Layer", subtitle: "beat: " + beat.kind,
          icon: "database", accent: acc,
        }});
        P.gauge(svg, {{
          x: 178, y: 694, w: 500,
          label: "PROGRESS",
          valueText: (ctx.beatIndex + 1) + " / " + ctx.beatCount,
          ratio: (ctx.beatIndex + 1) / Math.max(1, ctx.beatCount),
          accent: acc,
        }});
        if (beat.kind === "fail" && beat.failure_stamp) {{
          P.failureStamp(svg, {{ text: beat.failure_stamp, x: 428, y: 420, progress: Math.min(1, p * 2.4) }});
        }}
      }}

      window.CM.bootScene({{ renderStage: renderStage }});
    }})();
  </script>
</body>
</html>
"""


def scaffold_scene(
    slug: str,
    force: bool = False,
    root: Path | None = None,
) -> Path:
    """Create runs/<slug>/scene.html from runs/<slug>/blueprint.json."""
    state = load_state(slug, root)
    rdir = run_dir(slug, root)
    bp_path = rdir / "blueprint.json"
    if not bp_path.is_file():
        raise FileNotFoundError(f"Blueprint not found for run '{slug}': {bp_path}")

    try:
        blueprint = Blueprint.model_validate_json(bp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise ValueError(f"Invalid blueprint at {bp_path}: {exc}") from exc

    scene_path = rdir / "scene.html"
    if scene_path.is_file() and not force:
        raise FileExistsError(
            f"Scene already exists at {scene_path} (pass --force to overwrite)"
        )

    base_root = (root or get_repo_root()).resolve()
    eng_dir = resolve_engine_dir(root).resolve()
    if not eng_dir.is_relative_to(base_root):
        local_engine = base_root / "engine"
        local_engine.mkdir(parents=True, exist_ok=True)
        for asset in ("tokens.css", "runtime.js", "primitives.js"):
            src = eng_dir / asset
            dst = local_engine / asset
            if src.is_file() and not dst.exists():
                shutil.copy2(src, dst)
        eng_dir = local_engine.resolve()

    engine_base_url = os.path.relpath(eng_dir, rdir.resolve()).replace("\\", "/")

    html = render_starter_scene_html(blueprint, engine_base_url=engine_base_url)
    timeline_path = rdir / "audio" / "timeline.json"
    if timeline_path.is_file():
        html = inject_timeline_into_html(html, timeline_path.read_text(encoding="utf-8"))
    atomic_write_text(scene_path, html)

    current_scene_rec = state.stages.get(PipelineStage.SCENE)
    if current_scene_rec is None or current_scene_rec.status != StageStatus.PASSED or force:
        update_stage(
            slug=slug,
            stage=PipelineStage.SCENE,
            status=StageStatus.IN_PROGRESS,
            notes="Scaffolded starter scene.html",
            advance_on_pass=False,
            root=root,
        )

    return scene_path


def resolve_preview_target(
    target: str,
    root: Path | None = None,
) -> tuple[Path, Path]:
    """Resolve a run slug or HTML path into (serve_root, relative_html_path)."""
    base_root = (root or get_repo_root()).resolve()
    candidate = Path(target)
    looks_like_path = (
        target.lower().endswith((".html", ".htm"))
        or "/" in target
        or "\\" in target
        or candidate.exists()
    )

    if looks_like_path:
        if candidate.is_dir():
            raise ValueError(f"Expected a run slug or HTML file, got directory: {target}")
        resolved = candidate.resolve() if candidate.is_file() else (base_root / candidate).resolve()
        if not resolved.is_file():
            raise FileNotFoundError(f"Scene HTML file not found: {target}")
        if resolved.suffix.lower() not in (".html", ".htm"):
            raise ValueError(f"Preview target must be an .html file: {resolved}")
        if resolved.is_relative_to(base_root):
            return base_root, resolved.relative_to(base_root)
        return resolved.parent, Path(resolved.name)

    scene_path = (run_dir(target, root) / "scene.html").resolve()
    if not scene_path.is_file():
        raise FileNotFoundError(
            f"Scene not found for run '{target}': {scene_path} (run: cm scene init {target})"
        )
    return base_root, scene_path.relative_to(base_root)


class _PreviewHTTPRequestHandler(SimpleHTTPRequestHandler):
    """Quiet static server with /engine/ fallback for isolated temp-root previews."""

    def __init__(
        self,
        *args: object,
        directory: str,
        engine_fallback: Path | None = None,
        **kwargs: object,
    ) -> None:
        self._engine_fallback = engine_fallback
        super().__init__(*args, directory=directory, **kwargs)  # type: ignore[arg-type]

    def translate_path(self, path: str) -> str:
        translated = super().translate_path(path)
        clean_path = unquote(path.split("?", 1)[0].split("#", 1)[0])
        if (
            self._engine_fallback is not None
            and clean_path.startswith("/engine/")
            and not Path(translated).exists()
        ):
            rel = clean_path[len("/engine/") :].lstrip("/")
            candidate = (self._engine_fallback / rel).resolve()
            if candidate.is_relative_to(self._engine_fallback.resolve()):
                return str(candidate)
        return translated

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        _ = (format, args)


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


def create_preview_server(
    serve_root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    engine_fallback: Path | None = None,
) -> ThreadingHTTPServer:
    """Create a local ThreadingHTTPServer serving the scene workspace."""
    handler = partial(
        _PreviewHTTPRequestHandler,
        directory=str(serve_root.resolve()),
        engine_fallback=engine_fallback or resolve_engine_dir(),
    )
    return _ReusableThreadingHTTPServer((host, port), handler)
