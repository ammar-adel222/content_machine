/* Content Machine 3.0 — Deterministic 2D Scene Runtime & Layout Auditor */

(function () {
  "use strict";

  const FORMAT_SPECS = {
    short_9x16: {
      preset: "short_9x16",
      width: 1080,
      height: 1920,
      safe: { top: 160, bottom: 360, left: 64, right: 160 },
      stageViewBox: { width: 856, height: 840 },
      minFontPx: 28,
    },
    long_16x9: {
      preset: "long_16x9",
      width: 1920,
      height: 1080,
      safe: { top: 64, bottom: 96, left: 80, right: 80 },
      stageViewBox: { width: 1760, height: 560 },
      minFontPx: 20,
    },
  };

  function clamp(v, min, max) {
    return Math.max(min, Math.min(max, v));
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  function invLerp(a, b, v) {
    if (a === b) return 0;
    return clamp((v - a) / (b - a), 0, 1);
  }

  function remap(v, inMin, inMax, outMin, outMax) {
    return lerp(outMin, outMax, invLerp(inMin, inMax, v));
  }

  function easeOutCubic(t) {
    const x = clamp(t, 0, 1);
    return 1 - Math.pow(1 - x, 3);
  }

  function easeInOutCubic(t) {
    const x = clamp(t, 0, 1);
    return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2;
  }

  function easeOutExpo(t) {
    const x = clamp(t, 0, 1);
    return x >= 1 ? 1 : 1 - Math.pow(2, -10 * x);
  }

  function easeOutBack(t) {
    const x = clamp(t, 0, 1);
    const c1 = 1.70158;
    const c3 = c1 + 1;
    return 1 + c3 * Math.pow(x - 1, 3) + c1 * Math.pow(x - 1, 2);
  }

  function spring(tSec, opts) {
    if (tSec <= 0) return 0;
    const stiffness = (opts && opts.stiffness) || 240;
    const damping = (opts && opts.damping) || 18;
    const mass = (opts && opts.mass) || 1;
    const omega0 = Math.sqrt(stiffness / mass);
    const zeta = damping / (2 * Math.sqrt(stiffness * mass));
    if (zeta < 1) {
      const omegad = omega0 * Math.sqrt(1 - zeta * zeta);
      const decay = Math.exp(-zeta * omega0 * tSec);
      const val =
        1 -
        decay *
          (Math.cos(omegad * tSec) +
            ((zeta * omega0) / omegad) * Math.sin(omegad * tSec));
      return Math.abs(1 - val) < 0.0005 && tSec > 1.2 ? 1 : val;
    }
    const decay = Math.exp(-omega0 * tSec);
    return 1 - decay * (1 + omega0 * tSec);
  }

  function shake(tSec, duration, magnitude) {
    const dur = duration || 0.45;
    const mag = magnitude || 12;
    if (tSec < 0 || tSec >= dur) {
      return { x: 0, y: 0, rot: 0 };
    }
    const env = Math.pow(1 - tSec / dur, 2);
    return {
      x: Math.sin(tSec * 72) * mag * env,
      y: Math.cos(tSec * 58) * mag * 0.75 * env,
      rot: Math.sin(tSec * 64) * 0.9 * env,
    };
  }

  function seededRandom(seed) {
    let a = (seed >>> 0) || 1;
    return function () {
      a |= 0;
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function buildWordTimings(narration, startSec, speechEndSec, providedWords) {
    const tokens = String(narration || "")
      .trim()
      .split(/\s+/)
      .filter(Boolean);
    if (Array.isArray(providedWords) && providedWords.length > 0) {
      return providedWords.map(function (item) {
        return {
          word: item.word,
          startSec: Number(item.start_sec != null ? item.start_sec : item.startSec),
          endSec: Number(item.end_sec != null ? item.end_sec : item.endSec),
        };
      });
    }
    if (tokens.length === 0) return [];
    const span = Math.max(0.2, speechEndSec - startSec);
    const weights = tokens.map(function (tok) {
      return Math.max(2, tok.replace(/[^a-zA-Z0-9]/g, "").length);
    });
    const totalW = weights.reduce(function (acc, w) {
      return acc + w;
    }, 0);
    let cursor = startSec;
    return tokens.map(function (word, idx) {
      const share = (weights[idx] / totalW) * span;
      const entry = { word: word, startSec: cursor, endSec: cursor + share };
      cursor += share;
      return entry;
    });
  }

  function buildTimeline(blueprint, audioTimeline) {
    const wpm = (blueprint.voice && blueprint.voice.target_wpm) || 165;
    const audioMap = {};
    if (audioTimeline && Array.isArray(audioTimeline.beats)) {
      audioTimeline.beats.forEach(function (entry) {
        audioMap[entry.beat_id || entry.id] = entry;
      });
    }

    let cursor = 0;
    const versionProgression = [];
    const timedBeats = blueprint.beats.map(function (beat, index) {
      if (
        versionProgression.length === 0 ||
        versionProgression[versionProgression.length - 1] !== beat.version_label
      ) {
        versionProgression.push(beat.version_label);
      }
      const audioEntry = audioMap[beat.id];
      let startSec;
      let endSec;
      let speechEndSec;

      if (audioEntry) {
        startSec = Number(audioEntry.start_sec);
        endSec = Number(audioEntry.end_sec);
        speechEndSec =
          audioEntry.speech_end_sec != null
            ? Number(audioEntry.speech_end_sec)
            : Math.max(startSec + 0.2, endSec - (beat.pause_after_ms || 0) / 1000);
        cursor = endSec;
      } else {
        const pauseSec = (beat.pause_after_ms != null ? beat.pause_after_ms : 180) / 1000;
        const wordCount = String(beat.narration || "")
          .trim()
          .split(/\s+/)
          .filter(Boolean).length;
        const estTotal =
          beat.estimated_duration_sec != null
            ? Number(beat.estimated_duration_sec)
            : Math.max(1.0, wordCount / (wpm / 60)) + pauseSec;
        startSec = cursor;
        endSec = cursor + estTotal;
        speechEndSec = Math.max(startSec + 0.2, endSec - Math.min(pauseSec, estTotal * 0.4));
        cursor = endSec;
      }

      const words = buildWordTimings(
        beat.narration,
        startSec,
        speechEndSec,
        audioEntry && audioEntry.word_timings
      );

      return {
        index: index,
        beat: beat,
        startSec: startSec,
        endSec: endSec,
        speechEndSec: speechEndSec,
        durationSec: Math.max(0.05, endSec - startSec),
        words: words,
      };
    });

    return {
      beats: timedBeats,
      totalDuration: timedBeats.length > 0 ? timedBeats[timedBeats.length - 1].endSec : 0,
      versionProgression: versionProgression,
    };
  }

  function createEl(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text != null) el.textContent = text;
    return el;
  }

  function createHarness() {
    let state = {
      blueprint: null,
      spec: FORMAT_SPECS.short_9x16,
      timeline: null,
      renderStage: null,
      t: 0,
      beatIndex: 0,
      beatProgress: 0,
      runtimeErrors: [],
      dom: {},
      preview: {
        enabled: false,
        playing: false,
        rafId: null,
        lastPerf: null,
        safeOverlay: false,
      },
    };

    function installErrorTrap() {
      if (state._errorTrapInstalled) return;
      state._errorTrapInstalled = true;
      window.addEventListener("error", function (evt) {
        state.runtimeErrors.push(evt.message || String(evt.error || "Unknown error"));
      });
      window.addEventListener("unhandledrejection", function (evt) {
        state.runtimeErrors.push(String(evt.reason || "Unhandled promise rejection"));
      });
    }

    function buildDom(mountEl) {
      mountEl.innerHTML = "";
      const canvas = createEl("div", "cm-canvas");
      canvas.id = "cm-canvas";
      canvas.dataset.format = state.spec.preset;

      const vignette = createEl("div", "cm-breach-vignette");
      const safeFrame = createEl("div", "cm-safe-frame");
      safeFrame.id = "cm-safe-frame";

      // 1. Top HUD
      const hudTop = createEl("div", "cm-hud-top");
      const versionTrack = createEl("div", "cm-version-track");
      const versionChips = {};
      state.timeline.versionProgression.forEach(function (ver) {
        const chip = createEl("span", "cm-version-chip", ver);
        chip.dataset.version = ver;
        versionTrack.appendChild(chip);
        versionChips[ver] = chip;
      });

      const statusPill = createEl("div", "cm-status-pill");
      const statusDot = createEl("span", "cm-status-dot");
      const statusLabel = createEl("span", "cm-status-label", "INIT");
      statusPill.appendChild(statusDot);
      statusPill.appendChild(statusLabel);
      hudTop.appendChild(versionTrack);
      hudTop.appendChild(statusPill);

      // 2. Header band
      const headerBand = createEl("div", "cm-header-band");
      const headlineEl = createEl("h1", "cm-headline");
      const metricPill = createEl("div", "cm-metric-pill is-hidden");
      const metricLabel = createEl("span", "cm-metric-label");
      const metricValue = createEl("span", "cm-metric-value");
      metricPill.appendChild(metricLabel);
      metricPill.appendChild(metricValue);
      headerBand.appendChild(headlineEl);
      headerBand.appendChild(metricPill);

      // 3. Stage wrap
      const stageWrap = createEl("div", "cm-stage-wrap");
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.id = "cm-stage-svg";
      svg.setAttribute(
        "viewBox",
        "0 0 " + state.spec.stageViewBox.width + " " + state.spec.stageViewBox.height
      );
      svg.setAttribute("preserveAspectRatio", "xMidYMid meet");
      stageWrap.appendChild(svg);

      // 4. Captions band
      const captionsBand = createEl("div", "cm-captions-band");
      const captionBox = createEl("div", "cm-caption-box");
      captionsBand.appendChild(captionBox);

      safeFrame.appendChild(hudTop);
      safeFrame.appendChild(headerBand);
      safeFrame.appendChild(stageWrap);
      safeFrame.appendChild(captionsBand);

      // Safe-zone preview overlay
      const safeOverlay = createEl("div");
      safeOverlay.id = "cm-safe-overlay";
      [
        ["cm-unsafe-band cm-unsafe-top", "YT / IG Top UI"],
        ["cm-unsafe-band cm-unsafe-bottom", "Description / Audio Safe Margin"],
        ["cm-unsafe-band cm-unsafe-left", ""],
        ["cm-unsafe-band cm-unsafe-right", "UI"],
      ].forEach(function (pair) {
        safeOverlay.appendChild(createEl("div", pair[0], pair[1]));
      });

      canvas.appendChild(vignette);
      canvas.appendChild(safeFrame);
      canvas.appendChild(safeOverlay);

      if (state.preview.enabled) {
        const shell = createEl("div", "cm-preview-stage-shell");
        shell.appendChild(canvas);
        mountEl.appendChild(shell);
        state.dom.shell = shell;
      } else {
        mountEl.appendChild(canvas);
      }

      state.dom = Object.assign(state.dom, {
        mountEl: mountEl,
        canvas: canvas,
        vignette: vignette,
        safeFrame: safeFrame,
        versionChips: versionChips,
        statusPill: statusPill,
        statusDot: statusDot,
        statusLabel: statusLabel,
        headlineEl: headlineEl,
        metricPill: metricPill,
        metricLabel: metricLabel,
        metricValue: metricValue,
        stageWrap: stageWrap,
        svg: svg,
        captionBox: captionBox,
        safeOverlay: safeOverlay,
      });
    }

    function updatePreviewScale() {
      if (!state.preview.enabled || !state.dom.shell || !state.dom.canvas) return;
      const availW = Math.max(320, window.innerWidth - 64);
      const availH = Math.max(320, window.innerHeight - 150);
      const scale = Math.min(availW / state.spec.width, availH / state.spec.height, 1);
      state.dom.shell.style.width = Math.round(state.spec.width * scale) + "px";
      state.dom.shell.style.height = Math.round(state.spec.height * scale) + "px";
      state.dom.canvas.style.transform = "scale(" + scale.toFixed(4) + ")";
    }

    function mountPreviewBar() {
      const existing = document.getElementById("cm-preview-bar");
      if (existing) existing.remove();
      if (!state.preview.enabled) return;

      document.body.classList.add("cm-preview-mode");
      updatePreviewScale();
      window.addEventListener("resize", updatePreviewScale);

      const bar = createEl("div");
      bar.id = "cm-preview-bar";

      const controls = createEl("div", "cm-preview-controls");
      const btnPlay = createEl("button", "cm-preview-btn", "Play");
      btnPlay.id = "cm-btn-play";
      const btnPrev = createEl("button", "cm-preview-btn", "Prev Beat");
      btnPrev.id = "cm-btn-prev";
      const btnNext = createEl("button", "cm-preview-btn", "Next Beat");
      btnNext.id = "cm-btn-next";
      const btnSafe = createEl("button", "cm-preview-btn", "Safe Zones");
      btnSafe.id = "cm-btn-safe";
      const auditBadge = createEl("span", "cm-audit-badge is-pass", "AUDIT PASS");
      auditBadge.id = "cm-audit-badge";
      const readout = createEl("span", "cm-preview-readout");
      readout.id = "cm-preview-readout";

      controls.appendChild(btnPlay);
      controls.appendChild(btnPrev);
      controls.appendChild(btnNext);
      controls.appendChild(btnSafe);
      controls.appendChild(auditBadge);
      controls.appendChild(readout);

      const track = createEl("div", "cm-timeline-track");
      track.id = "cm-timeline-track";
      const total = Math.max(0.01, state.timeline.totalDuration);
      state.timeline.beats.forEach(function (tb) {
        const seg = createEl("div", "cm-timeline-seg");
        seg.style.width = ((tb.durationSec / total) * 100).toFixed(4) + "%";
        seg.dataset.kind = tb.beat.kind;
        seg.title = tb.beat.id + " (" + tb.beat.kind + ")";
        track.appendChild(seg);
      });
      const playhead = createEl("div", "cm-timeline-playhead");
      playhead.id = "cm-timeline-playhead";
      track.appendChild(playhead);

      bar.appendChild(controls);
      bar.appendChild(track);
      document.body.appendChild(bar);

      btnPlay.addEventListener("click", function () {
        state.preview.playing = !state.preview.playing;
        btnPlay.textContent = state.preview.playing ? "Pause" : "Play";
        if (state.preview.playing) {
          state.preview.lastPerf = performance.now();
          const tick = function (now) {
            if (!state.preview.playing) return;
            const dt = (now - state.preview.lastPerf) / 1000;
            state.preview.lastPerf = now;
            const nextT = state.t + dt;
            if (nextT >= state.timeline.totalDuration) {
              seek(state.timeline.totalDuration);
              state.preview.playing = false;
              btnPlay.textContent = "Play";
              return;
            }
            seek(nextT);
            state.preview.rafId = requestAnimationFrame(tick);
          };
          state.preview.rafId = requestAnimationFrame(tick);
        } else if (state.preview.rafId) {
          cancelAnimationFrame(state.preview.rafId);
        }
      });

      btnPrev.addEventListener("click", function () {
        prevBeat();
      });
      btnNext.addEventListener("click", function () {
        nextBeat();
      });
      btnSafe.addEventListener("click", function () {
        state.preview.safeOverlay = !state.preview.safeOverlay;
        btnSafe.classList.toggle("is-active", state.preview.safeOverlay);
        state.dom.safeOverlay.classList.toggle("is-visible", state.preview.safeOverlay);
      });

      function scrubAt(clientX) {
        const rect = track.getBoundingClientRect();
        const ratio = clamp((clientX - rect.left) / Math.max(1, rect.width), 0, 1);
        seek(ratio * state.timeline.totalDuration);
      }

      let dragging = false;
      track.addEventListener("mousedown", function (e) {
        dragging = true;
        scrubAt(e.clientX);
      });
      window.addEventListener("mousemove", function (e) {
        if (dragging) scrubAt(e.clientX);
      });
      window.addEventListener("mouseup", function () {
        dragging = false;
      });

      state.dom.previewReadout = readout;
      state.dom.playhead = playhead;
      state.dom.auditBadge = auditBadge;
    }

    function updateCaptions(timedBeat, t) {
      const box = state.dom.captionBox;
      box.innerHTML = "";
      const words = timedBeat.words;
      if (!words || words.length === 0) return;

      let activeIdx = 0;
      for (let i = 0; i < words.length; i += 1) {
        if (t >= words[i].startSec) activeIdx = i;
      }

      const chunkSize = 5;
      const chunkStart = Math.floor(activeIdx / chunkSize) * chunkSize;
      const slice = words.slice(chunkStart, chunkStart + chunkSize);

      slice.forEach(function (entry, offset) {
        const span = createEl("span", "cm-caption-word", entry.word);
        if (chunkStart + offset === activeIdx) {
          span.classList.add("is-active");
        }
        box.appendChild(span);
      });
    }

    function findBeatIndex(t) {
      const beats = state.timeline.beats;
      if (beats.length === 0) return 0;
      for (let i = 0; i < beats.length; i += 1) {
        if (t < beats[i].endSec || i === beats.length - 1) {
          return i;
        }
      }
      return beats.length - 1;
    }

    function seek(timeSec) {
      if (!state.timeline || state.timeline.beats.length === 0) {
        throw new Error("Harness not initialized with a valid timeline");
      }
      const total = state.timeline.totalDuration;
      const t = clamp(Number(timeSec) || 0, 0, total);
      const beatIndex = findBeatIndex(t);
      const timedBeat = state.timeline.beats[beatIndex];
      const beat = timedBeat.beat;
      const localTime = Math.max(0, t - timedBeat.startSec);
      const beatProgress = clamp(localTime / timedBeat.durationSec, 0, 1);
      const speechSpan = Math.max(0.1, timedBeat.speechEndSec - timedBeat.startSec);
      const speechProgress = clamp(localTime / speechSpan, 0, 1);

      state.t = t;
      state.beatIndex = beatIndex;
      state.beatProgress = beatProgress;

      // Update global CSS variables
      const rootStyle = state.dom.canvas.style;
      state.dom.canvas.dataset.kind = beat.kind;
      rootStyle.setProperty("--grid-offset-x", (-(t * 14) % 48).toFixed(2) + "px");
      rootStyle.setProperty("--grid-offset-y", ((t * 9) % 48).toFixed(2) + "px");

      // Fail beat camera shake & crimson vignette
      if (beat.kind === "fail") {
        const s = shake(localTime, 0.45, 10);
        const breachPulse = Math.max(0.45, 0.85 * Math.exp(-localTime * 1.6));
        rootStyle.setProperty("--shake-x", s.x.toFixed(2) + "px");
        rootStyle.setProperty("--shake-y", s.y.toFixed(2) + "px");
        rootStyle.setProperty("--shake-rot", s.rot.toFixed(2) + "deg");
        rootStyle.setProperty("--breach-intensity", breachPulse.toFixed(3));
      } else {
        rootStyle.setProperty("--shake-x", "0px");
        rootStyle.setProperty("--shake-y", "0px");
        rootStyle.setProperty("--shake-rot", "0deg");
        rootStyle.setProperty("--breach-intensity", "0");
      }

      // Version progression HUD
      const activeVerIdx = state.timeline.versionProgression.indexOf(beat.version_label);
      state.timeline.versionProgression.forEach(function (ver, idx) {
        const chip = state.dom.versionChips[ver];
        chip.className = "cm-version-chip";
        if (idx < activeVerIdx) {
          chip.classList.add("is-past");
        } else if (idx === activeVerIdx) {
          chip.classList.add("is-active");
          if (beat.kind === "fail") chip.classList.add("is-fail");
          if (beat.kind === "upgrade" || beat.kind === "payoff") chip.classList.add("is-upgrade");
        }
      });

      // Status pill + deterministic dot pulse
      state.dom.statusPill.dataset.status = beat.system_status;
      state.dom.statusLabel.textContent = beat.system_status.toUpperCase();
      const dotPulse = 0.72 + 0.28 * Math.sin(t * 7);
      state.dom.statusDot.style.opacity = dotPulse.toFixed(3);

      // Headline + spring metric badge
      state.dom.headlineEl.textContent = beat.on_screen_headline;
      if (beat.metric_callout) {
        const m = beat.metric_callout;
        state.dom.metricPill.classList.remove("is-hidden");
        state.dom.metricPill.dataset.severity = m.severity || "neutral";
        state.dom.metricLabel.textContent = m.label;
        state.dom.metricValue.textContent = m.value;
        const pop = clamp(spring(localTime, { stiffness: 280, damping: 20 }), 0.75, 1.08);
        state.dom.metricPill.style.transform = "scale(" + pop.toFixed(3) + ")";
      } else {
        state.dom.metricPill.classList.add("is-hidden");
      }

      updateCaptions(timedBeat, t);

      // Invoke scene stage renderer
      if (typeof state.renderStage === "function") {
        try {
          state.renderStage({
            t: t,
            totalDuration: total,
            beat: beat,
            beatIndex: beatIndex,
            beatCount: state.timeline.beats.length,
            localTime: localTime,
            beatProgress: beatProgress,
            speechProgress: speechProgress,
            svg: state.dom.svg,
            stageBox: state.spec.stageViewBox,
            spec: state.spec,
            blueprint: state.blueprint,
            timeline: state.timeline,
            CM: window.CM,
          });
        } catch (err) {
          state.runtimeErrors.push(err && err.message ? err.message : String(err));
        }
      }

      if (state.preview.enabled && state.dom.previewReadout) {
        state.dom.previewReadout.textContent =
          t.toFixed(2) +
          "s / " +
          total.toFixed(2) +
          "s | " +
          beat.id +
          " (" +
          beat.version_label +
          " " +
          beat.kind +
          ")";
        if (state.dom.playhead) {
          state.dom.playhead.style.left =
            ((t / Math.max(0.01, total)) * 100).toFixed(3) + "%";
        }
        if (state.dom.auditBadge) {
          const audit = auditLayout();
          state.dom.auditBadge.textContent = audit.passed
            ? "AUDIT PASS"
            : "AUDIT FAIL (" + audit.violations.length + ")";
          state.dom.auditBadge.className =
            "cm-audit-badge " + (audit.passed ? "is-pass" : "is-fail");
        }
      }

      return {
        t: t,
        totalDuration: total,
        beatId: beat.id,
        beatIndex: beatIndex,
        beatKind: beat.kind,
        versionLabel: beat.version_label,
        systemStatus: beat.system_status,
        beatProgress: beatProgress,
      };
    }

    function nextBeat() {
      const idx = Math.min(state.timeline.beats.length - 1, state.beatIndex + 1);
      return seek(state.timeline.beats[idx].startSec + 0.05);
    }

    function prevBeat() {
      const idx = Math.max(0, state.beatIndex - 1);
      return seek(state.timeline.beats[idx].startSec + 0.05);
    }

    function getState() {
      const timedBeat = state.timeline ? state.timeline.beats[state.beatIndex] : null;
      return {
        t: state.t,
        totalDuration: state.timeline ? state.timeline.totalDuration : 0,
        beatId: timedBeat ? timedBeat.beat.id : null,
        beatIndex: state.beatIndex,
        beatKind: timedBeat ? timedBeat.beat.kind : null,
        versionLabel: timedBeat ? timedBeat.beat.version_label : null,
        systemStatus: timedBeat ? timedBeat.beat.system_status : null,
        beatProgress: state.beatProgress,
        format: state.spec.preset,
        width: state.spec.width,
        height: state.spec.height,
        beatCount: state.timeline ? state.timeline.beats.length : 0,
        beats: state.timeline
          ? state.timeline.beats.map(function (b) {
              return {
                id: b.beat.id,
                kind: b.beat.kind,
                versionLabel: b.beat.version_label,
                startSec: b.startSec,
                endSec: b.endSec,
              };
            })
          : [],
        runtimeErrors: state.runtimeErrors.slice(),
      };
    }

    function getKeyframes() {
      if (!state.timeline) return [];
      const frames = [];
      state.timeline.beats.forEach(function (tb) {
        const pad = Math.min(0.12, tb.durationSec * 0.08);
        frames.push({
          beatId: tb.beat.id,
          beatIndex: tb.index,
          label: "start",
          t: Number((tb.startSec + pad).toFixed(4)),
        });
        frames.push({
          beatId: tb.beat.id,
          beatIndex: tb.index,
          label: "mid",
          t: Number((tb.startSec + tb.durationSec * 0.5).toFixed(4)),
        });
        frames.push({
          beatId: tb.beat.id,
          beatIndex: tb.index,
          label: "end",
          t: Number(Math.max(tb.startSec, tb.endSec - pad).toFixed(4)),
        });
      });
      return frames;
    }

    function isElementVisible(el) {
      const style = window.getComputedStyle(el);
      if (style.display === "none" || style.visibility === "hidden") return false;
      if (parseFloat(style.opacity) <= 0.01) return false;
      const rect = el.getBoundingClientRect();
      return rect.width > 0 || rect.height > 0;
    }

    function auditLayout() {
      const spec = state.spec;
      const canvas = state.dom.canvas;
      const svg = state.dom.svg;
      const safeFrame = state.dom.safeFrame;
      const violations = [];
      const timedBeat = state.timeline ? state.timeline.beats[state.beatIndex] : null;

      state.runtimeErrors.forEach(function (msg) {
        violations.push({ code: "RUNTIME_ERROR", element: "window", message: msg });
      });

      if (!canvas || !svg || !safeFrame) {
        violations.push({
          code: "MISSING_DOM",
          element: "cm-canvas",
          message: "Harness DOM is not mounted",
        });
        return {
          passed: false,
          t: state.t,
          beatId: timedBeat ? timedBeat.beat.id : null,
          viewport: { width: spec.width, height: spec.height },
          safeZone: spec.safe,
          minFontPx: spec.minFontPx,
          stageElementCount: 0,
          violations: violations,
        };
      }

      const canvasRect = canvas.getBoundingClientRect();
      const scaleX = canvasRect.width / spec.width;
      const scaleY = canvasRect.height / spec.height;
      const safeRect = {
        left: spec.safe.left - 2,
        right: spec.width - spec.safe.right + 2,
        top: spec.safe.top - 2,
        bottom: spec.height - spec.safe.bottom + 2,
      };

      const stagePrimitives = Array.from(
        svg.querySelectorAll("rect, circle, ellipse, line, polyline, polygon, path, text")
      ).filter(function (el) {
        if (el.closest("defs, marker, clipPath, mask, pattern")) return false;
        return isElementVisible(el);
      });

      if (stagePrimitives.length === 0) {
        violations.push({
          code: "EMPTY_STAGE",
          element: "cm-stage-svg",
          message: "Stage SVG has zero visible rendered primitives",
        });
      }

      const svgRect = svg.getBoundingClientRect();
      const vb = svg.viewBox.baseVal;
      const svgLogicalW = svgRect.width / Math.max(0.0001, scaleX);
      const svgLogicalH = svgRect.height / Math.max(0.0001, scaleY);
      const svgRenderScale =
        vb && vb.width > 0 && vb.height > 0
          ? Math.min(svgLogicalW / vb.width, svgLogicalH / vb.height)
          : 1;

      function checkSafeRect(el, label) {
        const rect = el.getBoundingClientRect();
        const lx1 = (rect.left - canvasRect.left) / scaleX;
        const ly1 = (rect.top - canvasRect.top) / scaleY;
        const lx2 = (rect.right - canvasRect.left) / scaleX;
        const ly2 = (rect.bottom - canvasRect.top) / scaleY;
        if (
          lx1 < safeRect.left ||
          lx2 > safeRect.right ||
          ly1 < safeRect.top ||
          ly2 > safeRect.bottom
        ) {
          violations.push({
            code: "SAFE_ZONE_OVERFLOW",
            element: label,
            message:
              label +
              " bounds [" +
              Math.round(lx1) +
              "," +
              Math.round(ly1) +
              " -> " +
              Math.round(lx2) +
              "," +
              Math.round(ly2) +
              "] exceed safe zone [" +
              spec.safe.left +
              "," +
              spec.safe.top +
              " -> " +
              (spec.width - spec.safe.right) +
              "," +
              (spec.height - spec.safe.bottom) +
              "]",
          });
        }
      }

      // Inspect HTML text and HUD blocks inside #cm-safe-frame
      Array.from(safeFrame.querySelectorAll("*")).forEach(function (el) {
        if (el === svg || el.closest("svg")) return;
        if (!isElementVisible(el)) return;

        const style = window.getComputedStyle(el);
        const hasDirectText = Array.from(el.childNodes).some(function (node) {
          return node.nodeType === 3 && node.textContent.trim().length > 0;
        });

        if (hasDirectText) {
          const fontSize = parseFloat(style.fontSize);
          if (fontSize < spec.minFontPx - 0.5) {
            violations.push({
              code: "FONT_TOO_SMALL",
              element: el.className || el.tagName.toLowerCase(),
              message:
                "Rendered font size " +
                fontSize.toFixed(1) +
                "px is below minimum " +
                spec.minFontPx +
                "px",
            });
          }
          checkSafeRect(el, el.className || el.tagName.toLowerCase());
        }

        if (
          el.clientWidth > 0 &&
          el.clientHeight > 0 &&
          (el.scrollWidth > el.clientWidth + 2 || el.scrollHeight > el.clientHeight + 2)
        ) {
          violations.push({
            code: "TEXT_CLIPPED",
            element: el.className || el.tagName.toLowerCase(),
            message:
              "Element content overflows its box (" +
              el.scrollWidth +
              "x" +
              el.scrollHeight +
              " > " +
              el.clientWidth +
              "x" +
              el.clientHeight +
              ")",
          });
        }
      });

      // Inspect SVG visible primitives & SVG <text> sizes
      stagePrimitives.forEach(function (el) {
        const tag = el.tagName.toLowerCase();
        checkSafeRect(el, "svg:" + tag);
        if (tag === "text" && el.textContent.trim().length > 0) {
          const rawFont = parseFloat(window.getComputedStyle(el).fontSize);
          const effectiveFont = rawFont * svgRenderScale;
          if (effectiveFont < spec.minFontPx - 0.5) {
            violations.push({
              code: "FONT_TOO_SMALL",
              element: "svg:text",
              message:
                "SVG text '" +
                el.textContent.trim().slice(0, 24) +
                "' effective size " +
                effectiveFont.toFixed(1) +
                "px is below minimum " +
                spec.minFontPx +
                "px",
            });
          }
        }
      });

      return {
        passed: violations.length === 0,
        t: state.t,
        beatId: timedBeat ? timedBeat.beat.id : null,
        viewport: { width: spec.width, height: spec.height },
        safeZone: spec.safe,
        minFontPx: spec.minFontPx,
        stageElementCount: stagePrimitives.length,
        violations: violations,
      };
    }

    function init(opts) {
      installErrorTrap();
      state.runtimeErrors = [];
      state.blueprint = opts.blueprint;
      state.spec = FORMAT_SPECS[opts.blueprint.format] || FORMAT_SPECS.short_9x16;
      state.timeline = buildTimeline(opts.blueprint, opts.audioTimeline || null);
      state.renderStage = opts.renderStage || null;

      const params = new URLSearchParams(window.location.search || "");
      state.preview.enabled =
        typeof opts.preview === "boolean" ? opts.preview : params.get("preview") === "1";

      const mountEl = opts.mountEl || document.getElementById("cm-root") || document.body;
      buildDom(mountEl);
      mountPreviewBar();
      seek(0);
      return getState();
    }

    return {
      init: init,
      seek: seek,
      getState: getState,
      getKeyframes: getKeyframes,
      auditLayout: auditLayout,
      nextBeat: nextBeat,
      prevBeat: prevBeat,
    };
  }

  function readJsonScript(id) {
    const el = document.getElementById(id);
    if (!el || !el.textContent.trim()) return null;
    return JSON.parse(el.textContent);
  }

  function bootScene(opts) {
    const blueprint = (opts && opts.blueprint) || readJsonScript("cm-blueprint-data");
    if (!blueprint) {
      throw new Error("Missing #cm-blueprint-data JSON script element");
    }
    const audioTimeline =
      (opts && opts.audioTimeline) ||
      window.__CM_TIMELINE__ ||
      readJsonScript("cm-timeline-data");
    const runInit = function () {
      window.__HARNESS__.init({
        blueprint: blueprint,
        audioTimeline: audioTimeline,
        renderStage: opts && opts.renderStage,
        mountEl: opts && opts.mountEl,
      });
    };
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", runInit);
    } else {
      runInit();
    }
  }

  window.CM = Object.assign(window.CM || {}, {
    FORMAT_SPECS: FORMAT_SPECS,
    math: {
      clamp: clamp,
      lerp: lerp,
      invLerp: invLerp,
      remap: remap,
      easeOutCubic: easeOutCubic,
      easeInOutCubic: easeInOutCubic,
      easeOutExpo: easeOutExpo,
      easeOutBack: easeOutBack,
      spring: spring,
      shake: shake,
      seededRandom: seededRandom,
    },
    buildTimeline: buildTimeline,
    bootScene: bootScene,
  });

  window.__HARNESS__ = createHarness();
})();
