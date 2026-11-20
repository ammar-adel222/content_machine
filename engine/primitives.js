/* Content Machine 3.0 — 2D Dark-Blueprint SVG Primitives */

(function () {
  "use strict";

  const SVG_NS = "http://www.w3.org/2000/svg";
  const FONT_SANS = '"Segoe UI", "Inter", system-ui, "Liberation Sans", sans-serif';
  const FONT_MONO = '"Cascadia Mono", "Consolas", "Liberation Mono", monospace';

  const ACCENTS = {
    cyan: { stroke: "#38bdf8", fill: "rgba(56, 189, 248, 0.14)", text: "#bae6fd" },
    emerald: { stroke: "#22c55e", fill: "rgba(34, 197, 94, 0.14)", text: "#bbf7d0" },
    amber: { stroke: "#f59e0b", fill: "rgba(245, 158, 11, 0.14)", text: "#fde68a" },
    crimson: { stroke: "#ef4444", fill: "rgba(239, 68, 68, 0.16)", text: "#fecaca" },
    violet: { stroke: "#a855f7", fill: "rgba(168, 85, 247, 0.16)", text: "#e9d5ff" },
    neutral: { stroke: "#94a3b8", fill: "rgba(148, 163, 184, 0.12)", text: "#e2e8f0" },
  };

  function el(tag, attrs, text) {
    const node = document.createElementNS(SVG_NS, tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        if (attrs[key] !== undefined && attrs[key] !== null) {
          node.setAttribute(key, String(attrs[key]));
        }
      });
    }
    if (text != null) node.textContent = String(text);
    return node;
  }

  function accentOf(name) {
    return ACCENTS[name] || ACCENTS.cyan;
  }

  function clear(svg) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    const defs = el("defs");

    const glow = el("filter", { id: "cm-glow", x: "-30%", y: "-30%", width: "160%", height: "160%" });
    glow.appendChild(el("feGaussianBlur", { stdDeviation: "4", result: "blur" }));
    const merge = el("feMerge");
    merge.appendChild(el("feMergeNode", { in: "blur" }));
    merge.appendChild(el("feMergeNode", { in: "SourceGraphic" }));
    glow.appendChild(merge);
    defs.appendChild(glow);

    Object.keys(ACCENTS).forEach(function (name) {
      const marker = el("marker", {
        id: "cm-arrow-" + name,
        viewBox: "0 0 12 12",
        refX: "10",
        refY: "6",
        markerWidth: "9",
        markerHeight: "9",
        orient: "auto-start-reverse",
      });
      marker.appendChild(el("path", { d: "M 1 2 L 11 6 L 1 10 z", fill: ACCENTS[name].stroke }));
      defs.appendChild(marker);
    });

    svg.appendChild(defs);
    return svg;
  }

  function appendIcon(group, iconName, cx, cy, color) {
    const iconG = el("g", {
      transform: "translate(" + cx + "," + cy + ")",
      stroke: color,
      "stroke-width": "2.6",
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      fill: "none",
    });

    if (iconName === "user") {
      iconG.appendChild(el("circle", { cx: 0, cy: -8, r: 7 }));
      iconG.appendChild(el("path", { d: "M -12 13 C -12 4 12 4 12 13" }));
    } else if (iconName === "database") {
      iconG.appendChild(el("ellipse", { cx: 0, cy: -10, rx: 13, ry: 5 }));
      iconG.appendChild(el("path", { d: "M -13 -10 V 10 C -13 14 13 14 13 10 V -10" }));
      iconG.appendChild(el("path", { d: "M -13 0 C -13 4 13 4 13 0" }));
    } else if (iconName === "shield") {
      iconG.appendChild(el("path", { d: "M 0 -14 L 13 -9 V 2 C 13 10 0 15 0 15 C 0 15 -13 10 -13 2 V -9 Z" }));
      iconG.appendChild(el("path", { d: "M -5 1 L -1 5 L 7 -4" }));
    } else if (iconName === "lock") {
      iconG.appendChild(el("rect", { x: -11, y: -2, width: 22, height: 16, rx: 4 }));
      iconG.appendChild(el("path", { d: "M -6 -2 V -8 C -6 -13 6 -13 6 -8 V -2" }));
    } else if (iconName === "unlock") {
      iconG.appendChild(el("rect", { x: -11, y: -2, width: 22, height: 16, rx: 4 }));
      iconG.appendChild(el("path", { d: "M -6 -2 V -9 C -6 -14 6 -14 7 -8" }));
    } else if (iconName === "hash") {
      iconG.appendChild(el("line", { x1: -6, y1: -13, x2: -8, y2: 13 }));
      iconG.appendChild(el("line", { x1: 7, y1: -13, x2: 5, y2: 13 }));
      iconG.appendChild(el("line", { x1: -13, y1: -4, x2: 13, y2: -4 }));
      iconG.appendChild(el("line", { x1: -13, y1: 5, x2: 13, y2: 5 }));
    } else if (iconName === "dice") {
      iconG.appendChild(el("rect", { x: -13, y: -13, width: 26, height: 26, rx: 5 }));
      iconG.appendChild(el("circle", { cx: -5, cy: -5, r: 2.2, fill: color }));
      iconG.appendChild(el("circle", { cx: 5, cy: 5, r: 2.2, fill: color }));
      iconG.appendChild(el("circle", { cx: 0, cy: 0, r: 2.2, fill: color }));
    } else if (iconName === "gpu" || iconName === "cpu") {
      iconG.appendChild(el("rect", { x: -10, y: -10, width: 20, height: 20, rx: 3 }));
      iconG.appendChild(el("rect", { x: -4, y: -4, width: 8, height: 8, rx: 1 }));
      [-5, 5].forEach(function (p) {
        iconG.appendChild(el("line", { x1: p, y1: -15, x2: p, y2: -10 }));
        iconG.appendChild(el("line", { x1: p, y1: 10, x2: p, y2: 15 }));
        iconG.appendChild(el("line", { x1: -15, y1: p, x2: -10, y2: p }));
        iconG.appendChild(el("line", { x1: 10, y1: p, x2: 15, y2: p }));
      });
    } else if (iconName === "attacker" || iconName === "warning") {
      iconG.appendChild(el("path", { d: "M 0 -14 L 14 12 H -14 Z" }));
      iconG.appendChild(el("line", { x1: 0, y1: -5, x2: 0, y2: 4 }));
      iconG.appendChild(el("circle", { cx: 0, cy: 8.5, r: 1.5, fill: color }));
    } else {
      iconG.appendChild(el("rect", { x: -13, y: -12, width: 26, height: 10, rx: 3 }));
      iconG.appendChild(el("rect", { x: -13, y: 2, width: 26, height: 10, rx: 3 }));
      iconG.appendChild(el("circle", { cx: -7, cy: -7, r: 1.6, fill: color }));
      iconG.appendChild(el("circle", { cx: -7, cy: 7, r: 1.6, fill: color }));
    }
    group.appendChild(iconG);
  }

  function node(svg, opts) {
    const x = opts.x;
    const y = opts.y;
    const w = opts.w || 270;
    const h = opts.h || 124;
    const acc = accentOf(opts.accent || "cyan");
    const opacity = opts.opacity != null ? opts.opacity : 1;
    const scale = opts.scale != null ? opts.scale : 1;
    const cx = x + w / 2;
    const cy = y + h / 2;

    const g = el("g", {
      opacity: opacity,
      transform:
        scale !== 1
          ? "translate(" + cx + "," + cy + ") scale(" + scale + ") translate(" + -cx + "," + -cy + ")"
          : null,
    });

    if (opts.pulse && opts.pulse > 0) {
      g.appendChild(
        el("rect", {
          x: x - 6,
          y: y - 6,
          width: w + 12,
          height: h + 12,
          rx: 22,
          fill: "none",
          stroke: acc.stroke,
          "stroke-width": 2,
          opacity: 0.35 * opts.pulse,
        })
      );
    }

    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: h,
        rx: 18,
        fill: "#0f172a",
        stroke: acc.stroke,
        "stroke-width": 3,
      })
    );

    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: h,
        rx: 18,
        fill: acc.fill,
      })
    );

    appendIcon(g, opts.icon || "server", x + 36, y + (opts.subtitle ? 44 : h / 2), acc.stroke);

    g.appendChild(
      el(
        "text",
        {
          x: x + 66,
          y: y + (opts.subtitle ? 52 : h / 2 + 10),
          fill: "#f8fafc",
          "font-family": FONT_SANS,
          "font-size": 30,
          "font-weight": 800,
        },
        opts.title
      )
    );

    if (opts.subtitle) {
      g.appendChild(
        el(
          "text",
          {
            x: x + 22,
            y: y + h - 22,
            fill: acc.text,
            "font-family": FONT_MONO,
            "font-size": 28,
            "font-weight": 700,
          },
          opts.subtitle
        )
      );
    }

    if (opts.badge) {
      const bw = Math.max(96, opts.badge.length * 19 + 24);
      const bx = x + w - bw - 14;
      const by = y - 22;
      g.appendChild(
        el("rect", {
          x: bx,
          y: by,
          width: bw,
          height: 40,
          rx: 12,
          fill: "#070b14",
          stroke: acc.stroke,
          "stroke-width": 2.5,
        })
      );
      g.appendChild(
        el(
          "text",
          {
            x: bx + bw / 2,
            y: by + 29,
            "text-anchor": "middle",
            fill: acc.text,
            "font-family": FONT_MONO,
            "font-size": 28,
            "font-weight": 800,
          },
          opts.badge
        )
      );
    }

    svg.appendChild(g);
    return g;
  }

  function quadPoint(p0, ctrl, p1, t) {
    const u = 1 - t;
    return {
      x: u * u * p0.x + 2 * u * t * ctrl.x + t * t * p1.x,
      y: u * u * p0.y + 2 * u * t * ctrl.y + t * t * p1.y,
    };
  }

  function pipe(svg, opts) {
    const from = opts.from;
    const to = opts.to;
    const acc = accentOf(opts.accent || "cyan");
    const progress = opts.progress != null ? Math.max(0, Math.min(1, opts.progress)) : 1;
    const curve = opts.curve || 0;
    const strokeWidth = opts.strokeWidth || 4;
    const midX = (from.x + to.x) / 2;
    const midY = (from.y + to.y) / 2 + curve;

    const d =
      curve !== 0
        ? "M " + from.x + " " + from.y + " Q " + midX + " " + midY + " " + to.x + " " + to.y
        : "M " + from.x + " " + from.y + " L " + to.x + " " + to.y;

    const g = el("g");
    const pathAttrs = {
      d: d,
      fill: "none",
      stroke: acc.stroke,
      "stroke-width": strokeWidth,
      "stroke-linecap": "round",
    };

    if (opts.dashed) {
      pathAttrs["stroke-dasharray"] = "12 10";
    } else if (progress < 1) {
      const approxLen = Math.hypot(to.x - from.x, to.y - from.y) + Math.abs(curve);
      pathAttrs["stroke-dasharray"] = approxLen;
      pathAttrs["stroke-dashoffset"] = approxLen * (1 - progress);
    }

    if (opts.arrow !== false && progress > 0.85) {
      pathAttrs["marker-end"] = "url(#cm-arrow-" + (opts.accent || "cyan") + ")";
    }

    g.appendChild(el("path", pathAttrs));

    if (opts.label && progress > 0.35) {
      const pt = curve !== 0 ? quadPoint(from, { x: midX, y: midY }, to, 0.5) : { x: midX, y: midY };
      const lw = Math.max(110, opts.label.length * 18 + 26);
      g.appendChild(
        el("rect", {
          x: pt.x - lw / 2,
          y: pt.y - 22,
          width: lw,
          height: 42,
          rx: 12,
          fill: "#070b14",
          stroke: acc.stroke,
          "stroke-width": 2,
        })
      );
      g.appendChild(
        el(
          "text",
          {
            x: pt.x,
            y: pt.y + 8,
            "text-anchor": "middle",
            fill: acc.text,
            "font-family": FONT_MONO,
            "font-size": 28,
            "font-weight": 700,
          },
          opts.label
        )
      );
    }

    svg.appendChild(g);
    return g;
  }

  function packetFlow(svg, opts) {
    const from = opts.from;
    const to = opts.to;
    const t = opts.t || 0;
    const count = opts.count || 3;
    const speed = opts.speed || 0.65;
    const acc = accentOf(opts.accent || "cyan");
    const radius = opts.radius || 9;
    const blockedAt = opts.blockedAt != null ? opts.blockedAt : null;
    const curve = opts.curve || 0;
    const ctrl = { x: (from.x + to.x) / 2, y: (from.y + to.y) / 2 + curve };

    const g = el("g");
    for (let i = 0; i < count; i += 1) {
      const phase = ((t * speed + i / count) % 1 + 1) % 1;
      let posT = phase;
      let opacity = 1;
      if (blockedAt !== null && phase > blockedAt) {
        posT = blockedAt;
        opacity = Math.max(0, 1 - (phase - blockedAt) * 4.5);
      }
      if (opacity <= 0.02) continue;
      const pt =
        curve !== 0
          ? quadPoint(from, ctrl, to, posT)
          : { x: from.x + (to.x - from.x) * posT, y: from.y + (to.y - from.y) * posT };

      g.appendChild(
        el("circle", {
          cx: pt.x,
          cy: pt.y,
          r: radius + 5,
          fill: acc.stroke,
          opacity: 0.28 * opacity,
        })
      );
      g.appendChild(
        el("circle", {
          cx: pt.x,
          cy: pt.y,
          r: radius,
          fill: "#ffffff",
          stroke: acc.stroke,
          "stroke-width": 3,
          opacity: opacity,
        })
      );
    }
    svg.appendChild(g);
    return g;
  }

  function tableCard(svg, opts) {
    const x = opts.x;
    const y = opts.y;
    const w = opts.w || 520;
    const rows = opts.rows || [];
    const cols = opts.columns || [];
    const rowH = 52;
    const headerH = 56;
    const colHeaderH = cols.length > 0 ? 44 : 0;
    const h = headerH + colHeaderH + rows.length * rowH + 14;
    const acc = accentOf(opts.accent || "cyan");
    const highlightSet = new Set(opts.highlightRows || []);

    const g = el("g", { opacity: opts.opacity != null ? opts.opacity : 1 });

    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: h,
        rx: 18,
        fill: "#0c1426",
        stroke: opts.cracked ? "#ef4444" : acc.stroke,
        "stroke-width": 3,
      })
    );

    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: headerH,
        rx: 18,
        fill: opts.cracked ? "rgba(239, 68, 68, 0.22)" : acc.fill,
      })
    );

    g.appendChild(
      el(
        "text",
        {
          x: x + 22,
          y: y + 38,
          fill: "#f8fafc",
          "font-family": FONT_SANS,
          "font-size": 30,
          "font-weight": 800,
        },
        opts.title
      )
    );

    const colStep = cols.length > 0 ? (w - 44) / cols.length : (w - 44) / 2;
    if (cols.length > 0) {
      cols.forEach(function (col, ci) {
        g.appendChild(
          el(
            "text",
            {
              x: x + 22 + ci * colStep,
              y: y + headerH + 31,
              fill: "#94a3b8",
              "font-family": FONT_MONO,
              "font-size": 28,
              "font-weight": 700,
            },
            col
          )
        );
      });
    }

    rows.forEach(function (row, ri) {
      const ry = y + headerH + colHeaderH + ri * rowH;
      const isHi = highlightSet.has(ri);
      if (isHi) {
        g.appendChild(
          el("rect", {
            x: x + 12,
            y: ry + 4,
            width: w - 24,
            height: rowH - 6,
            rx: 10,
            fill: "rgba(239, 68, 68, 0.24)",
            stroke: "#ef4444",
            "stroke-width": 2,
          })
        );
      }
      row.forEach(function (cell, ci) {
        g.appendChild(
          el(
            "text",
            {
              x: x + 22 + ci * colStep,
              y: ry + 36,
              fill: isHi ? "#fecaca" : "#e2e8f0",
              "font-family": FONT_MONO,
              "font-size": 28,
              "font-weight": 700,
            },
            cell
          )
        );
      });
    });

    if (opts.cracked) {
      g.appendChild(
        el("path", {
          d:
            "M " +
            (x + w * 0.12) +
            " " +
            (y + 10) +
            " L " +
            (x + w * 0.44) +
            " " +
            (y + h * 0.48) +
            " L " +
            (x + w * 0.36) +
            " " +
            (y + h * 0.56) +
            " L " +
            (x + w * 0.85) +
            " " +
            (y + h - 8),
          fill: "none",
          stroke: "#ef4444",
          "stroke-width": 5,
          "stroke-linecap": "round",
          "stroke-linejoin": "round",
        })
      );
    }

    svg.appendChild(g);
    return g;
  }

  function gauge(svg, opts) {
    const x = opts.x;
    const y = opts.y;
    const w = opts.w || 420;
    const ratio = Math.max(0, Math.min(1, opts.ratio != null ? opts.ratio : 0));
    const acc = accentOf(opts.accent || "cyan");
    const h = 88;

    const g = el("g", { opacity: opts.opacity != null ? opts.opacity : 1 });
    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: h,
        rx: 16,
        fill: "#0c1426",
        stroke: acc.stroke,
        "stroke-width": 2.5,
      })
    );
    g.appendChild(
      el(
        "text",
        {
          x: x + 18,
          y: y + 36,
          fill: "#cbd5e1",
          "font-family": FONT_MONO,
          "font-size": 28,
          "font-weight": 700,
        },
        opts.label
      )
    );
    g.appendChild(
      el(
        "text",
        {
          x: x + w - 18,
          y: y + 36,
          "text-anchor": "end",
          fill: acc.text,
          "font-family": FONT_MONO,
          "font-size": 28,
          "font-weight": 800,
        },
        opts.valueText
      )
    );

    const barW = w - 36;
    g.appendChild(
      el("rect", {
        x: x + 18,
        y: y + 52,
        width: barW,
        height: 20,
        rx: 10,
        fill: "rgba(148, 163, 184, 0.2)",
      })
    );
    if (ratio > 0) {
      g.appendChild(
        el("rect", {
          x: x + 18,
          y: y + 52,
          width: Math.max(16, barW * ratio),
          height: 20,
          rx: 10,
          fill: acc.stroke,
        })
      );
    }

    svg.appendChild(g);
    return g;
  }

  function codeCard(svg, opts) {
    const x = opts.x;
    const y = opts.y;
    const w = opts.w || 480;
    const lines = opts.lines || [];
    const acc = accentOf(opts.accent || "violet");
    const lineH = 44;
    const h = 54 + lines.length * lineH + 16;

    const g = el("g", { opacity: opts.opacity != null ? opts.opacity : 1 });
    g.appendChild(
      el("rect", {
        x: x,
        y: y,
        width: w,
        height: h,
        rx: 16,
        fill: "#090f1d",
        stroke: acc.stroke,
        "stroke-width": 2.5,
      })
    );
    g.appendChild(
      el(
        "text",
        {
          x: x + 20,
          y: y + 36,
          fill: acc.text,
          "font-family": FONT_MONO,
          "font-size": 28,
          "font-weight": 800,
        },
        opts.title
      )
    );

    lines.forEach(function (line, idx) {
      const ly = y + 54 + idx * lineH;
      if (idx === opts.highlightLine) {
        g.appendChild(
          el("rect", {
            x: x + 10,
            y: ly,
            width: w - 20,
            height: lineH - 4,
            rx: 8,
            fill: acc.fill,
          })
        );
      }
      g.appendChild(
        el(
          "text",
          {
            x: x + 20,
            y: ly + 30,
            fill: "#f8fafc",
            "font-family": FONT_MONO,
            "font-size": 28,
            "font-weight": 700,
          },
          line
        )
      );
    });

    svg.appendChild(g);
    return g;
  }

  function failureStamp(svg, opts) {
    const text = String(opts.text || "FAILED").toUpperCase();
    const x = opts.x;
    const y = opts.y;
    const angle = opts.angle != null ? opts.angle : -7;
    const progress = opts.progress != null ? opts.progress : 1;
    const acc = accentOf(opts.accent || "crimson");
    if (progress <= 0.01) return null;

    const springVal = window.CM.math.spring(progress * 0.7, { stiffness: 320, damping: 16 });
    const scale = Math.max(0.5, 1.45 - 0.45 * springVal);
    const w = Math.max(260, text.length * 24 + 54);
    const h = 76;

    const g = el("g", {
      transform:
        "translate(" +
        x +
        "," +
        y +
        ") rotate(" +
        angle +
        ") scale(" +
        scale.toFixed(3) +
        ")",
    });

    g.appendChild(
      el("rect", {
        x: -w / 2,
        y: -h / 2,
        width: w,
        height: h,
        rx: 14,
        fill: "rgba(127, 29, 29, 0.9)",
        stroke: acc.stroke,
        "stroke-width": 5,
      })
    );
    g.appendChild(
      el(
        "text",
        {
          x: 0,
          y: 12,
          "text-anchor": "middle",
          fill: "#ffffff",
          "font-family": FONT_MONO,
          "font-size": 36,
          "font-weight": 900,
          "letter-spacing": "2",
        },
        text
      )
    );
    svg.appendChild(g);
    return g;
  }

  function shieldAura(svg, opts) {
    const acc = accentOf(opts.accent || "emerald");
    const progress = opts.progress != null ? opts.progress : 1;
    const pulse = 0.65 + 0.35 * Math.sin((opts.t || 0) * 5);
    const rect = el("rect", {
      x: opts.x,
      y: opts.y,
      width: opts.w,
      height: opts.h,
      rx: 26,
      fill: "none",
      stroke: acc.stroke,
      "stroke-width": 3.5,
      "stroke-dasharray": "16 10",
      "stroke-dashoffset": -((opts.t || 0) * 32),
      opacity: Math.max(0.05, progress * pulse),
    });
    svg.appendChild(rect);
    return rect;
  }

  window.CM = window.CM || {};
  window.CM.primitives = {
    ACCENTS: ACCENTS,
    clear: clear,
    node: node,
    pipe: pipe,
    packetFlow: packetFlow,
    tableCard: tableCard,
    gauge: gauge,
    codeCard: codeCard,
    failureStamp: failureStamp,
    shieldAura: shieldAura,
  };
})();
