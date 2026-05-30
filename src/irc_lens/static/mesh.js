// irc-lens live agent-mesh graph.
//
// A vanilla-JS port of katvan's `MeshIsland.svelte` (the culture.dev
// "living mesh" Canvas 2D visualization). irc-lens has no JS build step,
// so the Svelte component is reimplemented here as a plain IIFE rather
// than imported/bundled. The rendering — breathing room/agent/human
// nodes, pulsing edges, message particles, force-settle-once layout — is
// faithful to the source; the differences are deliberate:
//
//   * data arrives at runtime via `LensMesh.update(data)` (fed by the
//     `mesh` SSE event in lens.js) instead of a build-time import;
//   * `update()` is a WARM rebuild — nodes that persist keep their
//     positions, only new nodes are seeded and gone nodes dropped, so a
//     live topology change never re-randomizes (and "jumps") the graph;
//   * the canvas fills its console pane (width × height) instead of
//     katvan's decorative fixed aspect ratio;
//   * colours are read from the page's `--lens-*` CSS tokens so the graph
//     matches the console theme (same palette katvan uses).
//
// Data shape (katvan's mesh.json contract):
//   { nodes: [{ id, label, kind, server }], edges: [{ source, target }] }
//   kind ∈ "room" | "agent" | "human"
(function () {
  "use strict";

  // ---- palette ---------------------------------------------------------
  function cssVar(name, fallback) {
    try {
      const v = getComputedStyle(document.documentElement)
        .getPropertyValue(name)
        .trim();
      return v || fallback;
    } catch (_e) {
      return fallback;
    }
  }

  function hexToRgb(hex) {
    const m = /^#?([0-9a-f]{6})$/i.exec((hex || "").trim());
    if (!m) return [65, 214, 122];
    const n = parseInt(m[1], 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  let COLORS, KIND, ACCENT_RGB, BRIGHT_RGB;

  function loadPalette() {
    COLORS = {
      surface: cssVar("--lens-surface", "#11161b"),
      text: cssVar("--lens-fg", "#f3f5f7"),
      muted: cssVar("--lens-muted", "#8a97a3"),
      accent: cssVar("--lens-accent", "#41d67a"),
      accentBright: cssVar("--lens-bright", "#7cff9e"),
    };
    ACCENT_RGB = hexToRgb(COLORS.accent);
    BRIGHT_RGB = hexToRgb(COLORS.accentBright);
    // Per-kind glyph styling — rooms are the larger hubs, agents glow,
    // humans read calmer/cooler so the social structure is legible.
    KIND = {
      room: { r: 16, fill: COLORS.surface, ring: COLORS.accentBright, core: COLORS.accentBright, dim: 0.85 },
      agent: { r: 11, fill: COLORS.surface, ring: COLORS.accent, core: COLORS.accent, dim: 1.0 },
      human: { r: 10, fill: COLORS.surface, ring: COLORS.muted, core: COLORS.text, dim: 0.7 },
    };
  }

  function rgba(rgb, a) {
    return "rgba(" + rgb[0] + "," + rgb[1] + "," + rgb[2] + "," + a + ")";
  }

  // ---- state -----------------------------------------------------------
  let canvas = null;
  let wrap = null;
  let ctx = null;
  let raf = 0;
  let ro = null; // ResizeObserver
  let io = null; // IntersectionObserver — pause when offscreen / hidden
  let running = false;
  let visible = true;
  let reduced = false;
  let glowSprite = null;
  let mounted = false;

  let W = 320;
  let H = 320;
  let layoutW = 0;
  let layoutH = 0;
  let t0 = 0;
  let lastT;
  let lastDraw = 0;
  const FRAME_INTERVAL = 1000 / 30; // cap the ambient loop at ~30fps
  const DRIFT_AMP = 7; // px of gentle float around the settled position

  // Simulation working state.
  let nodes = [];
  let edges = [];
  let particles = [];
  let byId = new Map();

  // Last data handed in (kept so update() before mount() still applies).
  let rawNodes = [];
  let rawEdges = [];
  let haveData = false;
  let prevSig = "";

  function rand(min, max) {
    return min + Math.random() * (max - min);
  }

  // Signature of the current topology — id set + edge set. Used to skip
  // a relayout when only the animation (not the graph) changed.
  function signature(rNodes, rEdges) {
    const ids = rNodes.map(function (n) { return n.id; }).sort();
    const es = rEdges
      .map(function (e) { return e.source + ">" + e.target; })
      .sort();
    return ids.join(",") + "|" + es.join(",");
  }

  // ---- data application (warm rebuild) --------------------------------
  function applyData() {
    const sig = signature(rawNodes, rawEdges);
    const topologyChanged = sig !== prevSig;
    const prev = byId; // id -> existing sim node

    const nextById = new Map();
    nodes = rawNodes.map(function (n) {
      const k = KIND[n.kind] || KIND.agent;
      const old = prev.get(n.id);
      let node;
      if (old) {
        // Carry over position + per-node animation phase so a persisting
        // node never teleports across a live update.
        node = old;
        node.label = n.label;
        node.kind = n.kind;
        node.server = n.server;
        node.r = k.r;
      } else {
        // New node: seed near centre with jitter; the short settle below
        // (topologyChanged) will pull it into place via the force sim.
        node = {
          id: n.id,
          label: n.label,
          kind: n.kind,
          server: n.server,
          x: W / 2 + rand(-60, 60),
          y: H / 2 + rand(-60, 60),
          vx: 0,
          vy: 0,
          r: k.r,
          phase: rand(0, Math.PI * 2),
          driftSpeed: rand(0.4, 0.9),
          glow: rand(0, Math.PI * 2),
        };
        node.bx = node.x;
        node.by = node.y;
      }
      nextById.set(n.id, node);
      return node;
    });
    byId = nextById;

    edges = rawEdges
      .map(function (e) { return { a: byId.get(e.source), b: byId.get(e.target) }; })
      .filter(function (e) { return e.a && e.b; })
      .map(function (e) {
        e.pulse = rand(0, Math.PI * 2);
        e.nextSpawn = rand(0.5, 4);
        return e;
      });

    // Particles reference old edge objects; drop them on a rebuild.
    particles = [];

    if (topologyChanged && nodes.length) {
      // Bounded settle, then freeze: existing nodes sit near equilibrium
      // and barely move, while new nodes get placed. Re-running the full
      // O(n^2) sim every frame is what made the source "slow when idle",
      // so we settle once here and only float/pulse per frame.
      for (let i = 0; i < 90; i++) step(0.05, 0);
      freezeLayout();
      layoutW = W;
      layoutH = H;
    }
    prevSig = sig;

    if (ctx) draw(0); // paint immediately (covers the reduced-motion path)
  }

  // ---- force layout (runs on rebuild + resize, NOT per frame) ---------
  function step(dt, time) {
    const cx = W / 2;
    const cy = H / 2;
    const REPEL = 5200;
    const SPRING = 0.012;
    const REST = 132;
    const CENTER = 0.0016;
    const DAMP = 0.86;

    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 1) d2 = 1;
        const d = Math.sqrt(d2);
        const f = REPEL / d2;
        const fx = (dx / d) * f;
        const fy = (dy / d) * f;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    for (const e of edges) {
      const dx = e.b.x - e.a.x;
      const dy = e.b.y - e.a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const f = (d - REST) * SPRING;
      const fx = (dx / d) * f;
      const fy = (dy / d) * f;
      e.a.vx += fx;
      e.a.vy += fy;
      e.b.vx -= fx;
      e.b.vy -= fy;
    }

    for (const n of nodes) {
      n.vx += (cx - n.x) * CENTER;
      n.vy += (cy - n.y) * CENTER;
      n.vx *= DAMP;
      n.vy *= DAMP;
      const drift = 9 * n.driftSpeed * dt;
      n.x += n.vx * dt * 6 + Math.cos(time * 0.5 * n.driftSpeed + n.phase) * drift;
      n.y += n.vy * dt * 6 + Math.sin(time * 0.4 * n.driftSpeed + n.phase) * drift;
      const pad = n.r + 8;
      if (n.x < pad) { n.x = pad; n.vx *= -0.4; }
      if (n.x > W - pad) { n.x = W - pad; n.vx *= -0.4; }
      if (n.y < pad) { n.y = pad; n.vy *= -0.4; }
      if (n.y > H - pad) { n.y = H - pad; n.vy *= -0.4; }
    }
  }

  function freezeLayout() {
    for (const n of nodes) {
      n.bx = n.x;
      n.by = n.y;
    }
  }

  function rescaleLayout() {
    if (!layoutW || !layoutH || !nodes.length) return;
    const sx = W / layoutW;
    const sy = H / layoutH;
    for (const n of nodes) {
      n.bx *= sx;
      n.by *= sy;
      n.x = n.bx;
      n.y = n.by;
    }
    layoutW = W;
    layoutH = H;
  }

  // ---- message particles ----------------------------------------------
  function maybeSpawn(dt) {
    for (const e of edges) {
      e.nextSpawn -= dt;
      if (e.nextSpawn <= 0) {
        e.nextSpawn = rand(3, 7);
        particles.push({
          edge: e,
          forward: Math.random() < 0.5,
          p: 0,
          speed: rand(0.16, 0.26),
        });
      }
    }
    for (const part of particles) part.p += part.speed * dt;
    particles = particles.filter(function (p) { return p.p < 1; });
  }

  function lerp(a, b, t) {
    return a + (b - a) * t;
  }

  // Pre-render the node glow once; draw() blits it per node (cheap
  // drawImage) instead of rebuilding a radial gradient each frame.
  function makeGlowSprite() {
    const s = 64;
    const c = document.createElement("canvas");
    c.width = c.height = s;
    const g = c.getContext("2d");
    const grad = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    grad.addColorStop(0, rgba(ACCENT_RGB, 1));
    grad.addColorStop(1, rgba(ACCENT_RGB, 0));
    g.fillStyle = grad;
    g.fillRect(0, 0, s, s);
    glowSprite = c;
  }

  // ---- paint -----------------------------------------------------------
  function draw(time) {
    if (!ctx) return;

    for (const n of nodes) {
      n.dx = (n.bx || n.x) + Math.cos(time * 0.22 * n.driftSpeed + n.phase) * DRIFT_AMP;
      n.dy = (n.by || n.y) + Math.sin(time * 0.18 * n.driftSpeed + n.phase) * DRIFT_AMP;
    }

    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = COLORS.surface;
    ctx.fillRect(0, 0, W, H);

    // Edges — gentle per-edge brightness pulse.
    ctx.lineWidth = 1;
    for (const e of edges) {
      const pulse = (Math.sin(time * 0.35 + e.pulse) + 1) / 2;
      ctx.strokeStyle = rgba(ACCENT_RGB, 0.14 + pulse * 0.22);
      ctx.beginPath();
      ctx.moveTo(e.a.dx, e.a.dy);
      ctx.lineTo(e.b.dx, e.b.dy);
      ctx.stroke();
    }

    // Message particles travelling along edges.
    for (const part of particles) {
      const e = part.edge;
      const from = part.forward ? e.a : e.b;
      const to = part.forward ? e.b : e.a;
      const x = lerp(from.dx, to.dx, part.p);
      const y = lerp(from.dy, to.dy, part.p);
      const a = Math.sin(part.p * Math.PI);
      const g = ctx.createRadialGradient(x, y, 0, x, y, 6);
      g.addColorStop(0, rgba(BRIGHT_RGB, 0.9 * a));
      g.addColorStop(1, rgba(BRIGHT_RGB, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(x, y, 6, 0, Math.PI * 2);
      ctx.fill();
    }

    // Nodes — breathing ring, glowing core, label.
    for (const n of nodes) {
      const k = KIND[n.kind] || KIND.agent;
      const breathe = 1 + Math.sin(time * 0.7 + n.phase) * 0.05;
      const r = n.r * breathe;
      const glow = (Math.sin(time * 0.8 + n.glow) + 1) / 2;

      if (glowSprite) {
        const hr = r * 2.4;
        ctx.globalAlpha = 0.18 * k.dim;
        ctx.drawImage(glowSprite, n.dx - hr, n.dy - hr, hr * 2, hr * 2);
        ctx.globalAlpha = 1;
      }

      ctx.beginPath();
      ctx.arc(n.dx, n.dy, r, 0, Math.PI * 2);
      ctx.fillStyle = k.fill;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = k.ring;
      ctx.stroke();

      ctx.beginPath();
      ctx.arc(n.dx, n.dy, Math.max(2, r * 0.28), 0, Math.PI * 2);
      ctx.globalAlpha = 0.7 + glow * 0.3;
      ctx.fillStyle = k.core;
      ctx.fill();
      ctx.globalAlpha = 1;

      ctx.font = '600 11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      ctx.fillStyle = n.kind === "room" ? COLORS.accentBright : COLORS.text;
      ctx.fillText(n.label, n.dx, n.dy + r + 4);
    }

    // Empty-state hint so the pane never looks broken before data lands.
    if (!nodes.length) {
      ctx.fillStyle = COLORS.muted;
      ctx.font = '12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText("no agents in view — /join a channel", W / 2, H / 2);
    }
  }

  // ---- loop ------------------------------------------------------------
  function frame(now) {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    if (now - lastDraw < FRAME_INTERVAL) return;
    lastDraw = now;
    const time = (now - t0) / 1000;
    const dt = Math.min(0.05, time - (lastT == null ? time : lastT));
    lastT = time;
    maybeSpawn(dt);
    draw(time);
  }

  function start() {
    if (running || reduced || !visible || !mounted) return;
    if (typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(raf);
    running = true;
    t0 = performance.now();
    lastT = undefined;
    lastDraw = 0;
    raf = requestAnimationFrame(frame);
  }

  function stop() {
    running = false;
    if (typeof cancelAnimationFrame !== "undefined") cancelAnimationFrame(raf);
  }

  function renderStatic() {
    draw(0);
  }

  // ---- sizing ----------------------------------------------------------
  function resize() {
    if (!canvas || !wrap) return;
    // Measure the WRAP (sized by the grid), never the canvas itself —
    // the canvas is CSS width/height:100%, so measuring it back into its
    // own backing store would create a feedback loop. Hidden panes report
    // 0; we clamp and rescale once the pane becomes visible.
    W = Math.max(160, Math.round(wrap.clientWidth || 320));
    H = Math.max(160, Math.round(wrap.clientHeight || 320));
    const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
    canvas.width = Math.round(W * dpr);
    canvas.height = Math.round(H * dpr);
    ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }

  // ---- public API ------------------------------------------------------
  function update(data) {
    rawNodes = (data && Array.isArray(data.nodes)) ? data.nodes : [];
    rawEdges = (data && Array.isArray(data.edges)) ? data.edges : [];
    haveData = true;
    if (mounted) applyData();
  }

  function mount(canvasEl) {
    if (!canvasEl || mounted) return;
    canvas = canvasEl;
    wrap = canvasEl.parentElement || canvasEl;
    loadPalette();

    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    reduced = mq.matches;

    resize();
    makeGlowSprite();
    mounted = true;
    if (haveData) applyData();
    draw(0);

    ro = new ResizeObserver(function () {
      const had = W;
      const hadH = H;
      resize();
      if (W !== had || H !== hadH) rescaleLayout();
      draw(0);
    });
    ro.observe(wrap);

    // Pause whenever the canvas isn't on screen — scrolled away, the tab
    // hidden, OR the mesh pane is display:none in another view. A loop
    // that runs forever is what exhausts GPU/memory over a long session.
    io = new IntersectionObserver(
      function (entries) {
        visible = entries[0] ? entries[0].isIntersecting : true;
        if (!visible) stop();
        else start();
      },
      { threshold: 0 }
    );
    io.observe(wrap);

    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stop();
      else start();
    });

    const onPref = function (e) {
      reduced = e.matches;
      if (reduced) { stop(); renderStatic(); }
      else start();
    };
    if (mq.addEventListener) mq.addEventListener("change", onPref);
    else if (mq.addListener) mq.addListener(onPref);
  }

  window.LensMesh = { mount: mount, update: update };

  // Auto-mount when the DOM is ready (the console ships exactly one
  // mesh canvas). Guard for environments without the element.
  function autoMount() {
    const el = document.getElementById("mesh-canvas");
    if (el) mount(el);
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", autoMount);
  } else {
    autoMount();
  }
})();
