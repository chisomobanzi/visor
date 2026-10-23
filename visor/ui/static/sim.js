/* SIMULATOR + PLAYTESTS: drive apps in Visor's virtual headset.
   Uses $, api, toast and fail from the main dashboard script. */
"use strict";
(() => {
  const S = {
    current: null,        // instance name being driven
    apps: [],             // /api/xr/running
    paused: false,
    events: 0,            // last event seq seen
    fpsHistory: [],
    refresh: 72,
    launching: null,      // name of an app still booting
    streamKey: null,
    dims: { w: 960, h: 1006 },
    rec: null,
    personas: [],
    persona: "first-timer",
    playtest: null,       // { name, since }
  };
  const post = (path, body) => api(path, {
    method: "POST",
    headers: body ? { "content-type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const clock = () => new Date().toLocaleTimeString([], { hour12: false });
  const active = () => location.hash === "#sim";
  const xr = (path) => `/api/xr/${encodeURIComponent(S.current)}${path}`;

  /* ---------------- instances ---------------- */
  async function refreshApps() {
    try { S.apps = await api("/api/xr/running"); } catch { S.apps = []; }
    if (S.current && !S.apps.some(a => a.name === S.current) && S.launching !== S.current) S.current = null;
    if (!S.current && S.apps.length && !S.launching) select(S.apps[0].name);
    renderTabs();
    $("#sim-empty").hidden = !!(S.apps.length || S.launching || !$("#sim-launch").hidden);
    $("#sim-stage").hidden = !S.current;
  }
  function renderTabs() {
    const tabs = $("#sim-tabs");
    tabs.innerHTML = S.apps.map(a => {
      const cls = ["sim-tab", a.name === S.current ? "sel" : "", a.paused ? "paused" : (a.session === "focused" ? "live" : "")].join(" ");
      return `<button class="${cls}" role="tab" aria-selected="${a.name === S.current}" data-name="${esc(a.name)}"
        title="${esc(a.name)}"><span class="dot"></span><b>${esc(a.app || a.name)}</b>${esc(a.fps ? a.fps.toFixed(0) + " fps" : a.session || "")}</button>`;
    }).join("");
    tabs.querySelectorAll(".sim-tab").forEach(b => b.onclick = () => select(b.dataset.name));
  }
  function select(name) {
    if (S.current === name && S.streamKey === name) return;
    S.current = name; S.events = 0; S.fpsHistory = []; S.saidCount = -1;
    S.mine = []; S.theirs = []; S.heardSeq = 0; listenLive(false);
    $("#sim-said").innerHTML = '<li class="empty">The app hears only what you say here — never this computer\'s microphone. What it says back appears here too.</li>';
    $("#sim-feed").innerHTML = '<li class="empty">Haptic pulses and session changes show up here.</li>';
    $("#sim-stage").hidden = false; $("#sim-empty").hidden = true;
    startStream(); loadInfo(); pollMic(); renderTabs();
  }

  /* ---------------- launch ---------------- */
  async function loadSuggest() {
    try {
      const items = await api("/api/xr/suggest");
      $("#sl-suggest").innerHTML = items.map(i => `<option value="${esc(i.target)}">${esc(i.label)}</option>`).join("");
      const ue = items.find(i => i.kind === "unreal");
      if (ue) { $("#sl-target").placeholder = ue.target; $("#pt-target").placeholder = ue.target; }
    } catch {}
  }
  function openLaunch(open = true) {
    $("#sim-launch").hidden = !open;
    if (open) { $("#sim-empty").hidden = true; $("#sl-target").focus(); }
    else refreshApps();
  }
  async function launch(target) {
    target = target || $("#sl-target").value.trim() || $("#sl-target").placeholder;
    if (!target) return toast("Pick an app to launch.", true);
    const note = $("#sl-progress"); note.className = "note";
    $("#sl-go").disabled = true;
    try {
      const r = await post("/api/xr/launch", {
        target, map: $("#sl-map").value.trim() || null, headset: $("#sl-headset").value,
        scale: parseFloat($("#sl-scale").value), paused: $("#sl-paused").checked,
        seated: $("#sl-seated").checked, mic: $("#sl-mic").checked,
      });
      S.launching = r.name;
      $("#sim-launch").hidden = false; $("#sim-empty").hidden = true;
      note.textContent = `starting ${r.app}…`;
      for (let i = 0; i < 900; i++) {
        await new Promise(res => setTimeout(res, 1000));
        const p = await api(`/api/xr/launching/${encodeURIComponent(r.name)}`);
        if (p.ready) { S.launching = null; note.textContent = ""; openLaunch(false); await refreshApps(); select(r.name); toast(`${r.app} is running in the virtual headset`); break; }
        if (p.failed) { S.launching = null; note.className = "note err"; note.textContent = p.note || "the app exited"; break; }
        note.textContent = `${r.app}: ${p.note}${i > 20 ? " — first launches compile shaders, this can take a few minutes" : ""}`;
      }
    } catch (e) { note.className = "note err"; note.textContent = e.message; S.launching = null; }
    finally { $("#sl-go").disabled = false; }
  }

  /* ---------------- live view ---------------- */
  function startStream() {
    const img = $("#sim-img");
    S.streamKey = S.current;
    $("#sim-waiting").hidden = false;
    $("#sim-waiting").textContent = "connecting…";
    // MJPEG streams don't fire "load" per frame in every browser; watch the
    // decoded size instead to know when pictures are arriving.
    clearInterval(S.sizeWatch);
    S.sizeWatch = setInterval(() => {
      if (!img.naturalWidth) return;
      $("#sim-waiting").hidden = true;
      if (img.naturalWidth !== S.dims.w || img.naturalHeight !== S.dims.h) {
        S.dims = { w: img.naturalWidth, h: img.naturalHeight };
        $("#simview").style.aspectRatio = `${img.naturalWidth} / ${img.naturalHeight}`;
      }
    }, 250);
    img.onerror = () => { $("#sim-waiting").hidden = false; $("#sim-waiting").textContent = "no picture — is the app still running?"; };
    img.src = `${xr("/stream")}?fps=24&width=960&t=${Date.now()}`;
  }
  function stopStream() { clearInterval(S.sizeWatch); $("#sim-img").removeAttribute("src"); S.streamKey = null; }

  function viewPoint(ev) {
    const view = $("#simview"), r = view.getBoundingClientRect();
    // object-fit: contain — find the drawn image rect inside the box
    const scale = Math.min(r.width / S.dims.w, r.height / S.dims.h);
    const dw = S.dims.w * scale, dh = S.dims.h * scale;
    const ox = (r.width - dw) / 2, oy = (r.height - dh) / 2;
    const x = (ev.clientX - r.left - ox) / scale, y = (ev.clientY - r.top - oy) / scale;
    return { x, y, inside: x >= 0 && y >= 0 && x <= S.dims.w && y <= S.dims.h, px: ev.clientX - r.left, py: ev.clientY - r.top };
  }
  function ripple(px, py, cls) {
    const d = document.createElement("div");
    d.className = "ripple " + (cls || ""); d.style.left = px + "px"; d.style.top = py + "px";
    $("#simview").appendChild(d); setTimeout(() => d.remove(), 600);
  }

  // Drag to look, click to press.
  let drag = null, lookAcc = { y: 0, p: 0 }, lookTimer = null;
  function flushLook() {
    if (!S.current || (!lookAcc.y && !lookAcc.p)) return;
    const body = { dyaw: lookAcc.y, dpitch: lookAcc.p, over: 2 };
    lookAcc = { y: 0, p: 0 };
    post(xr("/look"), body).catch(() => {});
  }
  const view = $("#simview");
  view.addEventListener("pointerdown", (ev) => {
    if (!S.current || ev.button > 0) return;
    view.setPointerCapture(ev.pointerId);
    drag = { x: ev.clientX, y: ev.clientY, moved: false, ev };
    view.focus();
  });
  view.addEventListener("pointermove", (ev) => {
    if (!drag) return;
    const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    drag.moved = true; view.classList.add("dragging");
    lookAcc.y -= dx * 0.18; lookAcc.p -= dy * 0.18;
    drag.x = ev.clientX; drag.y = ev.clientY;
    if (!lookTimer) lookTimer = setInterval(flushLook, 50);
  });
  view.addEventListener("pointerup", async (ev) => {
    if (!drag) return;
    const wasDrag = drag.moved; drag = null; view.classList.remove("dragging");
    if (wasDrag) { flushLook(); clearInterval(lookTimer); lookTimer = null; return; }
    const p = viewPoint(ev);
    if (!p.inside) return;
    const hand = ev.shiftKey ? "left" : "right";
    const pressBtn = !ev.altKey;
    ripple(p.px, p.py, (hand === "left" ? "left " : "") + (pressBtn ? "" : "point"));
    try { await post(xr("/click"), { x: p.x, y: p.y, width: S.dims.w, height: S.dims.h, hand, press: pressBtn }); }
    catch (e) { fail(e); }
  });
  view.addEventListener("contextmenu", (e) => e.preventDefault());

  /* ---------------- controllers ---------------- */
  document.querySelectorAll(".hand").forEach(handEl => {
    const hand = handEl.dataset.hand;
    handEl.querySelectorAll("button[data-b]").forEach(btn => {
      const b = btn.dataset.b;
      btn.addEventListener("pointerdown", async () => {
        if (!S.current) return;
        btn.classList.add("down");
        if (S.paused) { try { await post(xr("/press"), { hand, button: b, frames: 6 }); } catch (e) { fail(e); } }
        else post(xr("/hold"), { hand, button: b, down: true }).catch(fail);
      });
      const up = () => {
        if (!btn.classList.contains("down")) return;
        btn.classList.remove("down");
        if (S.current && !S.paused) post(xr("/hold"), { hand, button: b, down: false }).catch(() => {});
      };
      btn.addEventListener("pointerup", up); btn.addEventListener("pointerleave", up);
    });
    const pad = handEl.querySelector(".stickpad"), knob = pad.querySelector("i");
    let last = 0;
    const send = (x, y) => { if (S.current) post(xr("/stick"), { hand, x, y, frames: 0 }).catch(() => {}); };
    const move = (ev) => {
      const r = pad.getBoundingClientRect(), R = r.width / 2;
      let dx = ev.clientX - (r.left + R), dy = ev.clientY - (r.top + R);
      const len = Math.hypot(dx, dy), max = R - 17;
      if (len > max) { dx *= max / len; dy *= max / len; }
      knob.style.transform = `translate(${dx}px, ${dy}px)`;
      const now = performance.now();
      if (now - last > 60) { last = now; send(+(dx / max).toFixed(2), +(-dy / max).toFixed(2)); }
    };
    pad.addEventListener("pointerdown", (ev) => { pad.setPointerCapture(ev.pointerId); pad.classList.add("active"); move(ev); });
    pad.addEventListener("pointermove", (ev) => { if (pad.classList.contains("active")) move(ev); });
    const release = () => { if (!pad.classList.contains("active")) return; pad.classList.remove("active"); knob.style.transform = ""; send(0, 0); };
    pad.addEventListener("pointerup", release); pad.addEventListener("pointercancel", release);
  });

  /* ---------------- transport ---------------- */
  $("#sim-pp").onclick = async () => {
    if (!S.current) return;
    try { await post(xr(S.paused ? "/resume" : "/pause")); S.paused = !S.paused; renderPaused(); } catch (e) { fail(e); }
  };
  document.querySelectorAll("#sim [data-step]").forEach(b => b.onclick = async () => {
    if (!S.current) return;
    try { const r = await post(`${xr("/step")}?frames=${b.dataset.step}`); S.paused = true; renderPaused(); $("#sim-frame").textContent = `frame ${r.frame}`; }
    catch (e) { fail(e); }
  });
  document.querySelectorAll("#sim [data-ev]").forEach(b => b.onclick = async () => {
    if (!S.current) return;
    let ev = b.dataset.ev;
    if (ev === "focus_lost" && b.dataset.open === "1") ev = "focus_gained";
    try {
      await post(`${xr("/event")}?event=${ev}`);
      if (b.dataset.ev === "focus_lost") {
        b.dataset.open = ev === "focus_lost" ? "1" : "0";
        b.textContent = ev === "focus_lost" ? "CLOSE MENU" : "META MENU";
        b.classList.toggle("on", ev === "focus_lost");
      }
      toast(ev === "focus_lost" ? "Meta menu opened — the app lost focus and should pause"
        : ev === "focus_gained" ? "Meta menu closed" : "Recentered");
    } catch (e) { fail(e); }
  });
  $("#sim-shot").onclick = () => { if (S.current) window.open(`${xr("/shot")}?width=1680`, "_blank"); };
  $("#sim-stop").onclick = async () => {
    if (!S.current || !confirmStop()) return;
    const name = S.current;
    stopStream();
    try { await post(`/api/xr/${encodeURIComponent(name)}/stop`); toast("stopped"); } catch (e) { fail(e); }
    S.current = null; refreshApps();
  };
  function confirmStop() { return true; }  // stopping is cheap to undo: relaunch
  $("#sim-rec").onclick = async () => {
    if (!S.current) return;
    const btn = $("#sim-rec"), note = $("#sim-rec-note");
    try {
      if (!S.rec) {
        await post(xr("/record/start"));
        S.rec = { name: S.current, t0: Date.now() };
        btn.classList.add("rec-on"); btn.textContent = "■ STOP";
        S.recTick = setInterval(() => { note.textContent = `recording ${((Date.now() - S.rec.t0) / 1000).toFixed(0)}s`; }, 500);
      } else {
        const r = await post(`/api/xr/${encodeURIComponent(S.rec.name)}/record/stop`);
        clearInterval(S.recTick); S.rec = null;
        btn.classList.remove("rec-on"); btn.textContent = "● REC";
        note.innerHTML = `<a href="${esc(r.url)}" target="_blank">${r.seconds.toFixed(0)} s video</a>${r.replay ? " + replay script" : ""}`;
        toast(`Saved ${r.video}`);
      }
    } catch (e) { fail(e); }
  };

  function renderPaused() {
    $("#sim-paused").hidden = !S.paused;
    $("#sim-pp").textContent = S.paused ? "▶ RESUME" : "❚❚ PAUSE";
  }

  /* ---------------- keyboard ---------------- */
  const held = new Set();
  window.addEventListener("keydown", async (e) => {
    if (!active() || !S.current || e.target.matches("input, textarea, select") || e.metaKey || e.ctrlKey) return;
    const k = e.key.toLowerCase();
    const walk = { w: [0.25, 0], s: [-0.25, 0], a: [0, -0.25], d: [0, 0.25] }[k];
    try {
      if (walk) { e.preventDefault(); await post(xr("/walk"), { forward: walk[0], right: walk[1], over: 14 }); return; }
      if (k === "q" || k === "e") { e.preventDefault(); await post(xr("/look"), { dyaw: k === "q" ? 15 : -15, over: 10 }); return; }
      if (k.startsWith("arrow")) {
        e.preventDefault();
        const d = { arrowleft: [6, 0], arrowright: [-6, 0], arrowup: [0, 5], arrowdown: [0, -5] }[k];
        await post(xr("/look"), { dyaw: d[0], dpitch: d[1], over: 4 }); return;
      }
      if (k === "p") { $("#sim-pp").click(); return; }
      if (k === ".") { document.querySelector('#sim [data-step="1"]').click(); return; }
      if (k === ">") { document.querySelector('#sim [data-step="72"]').click(); return; }
      const btn = { " ": ["right", "trigger"], g: ["right", "grip"], "1": ["right", "a"], "2": ["right", "b"],
        "3": ["left", "x"], "4": ["left", "y"], m: ["left", "menu"] }[k];
      if (btn && !held.has(k)) {
        e.preventDefault(); held.add(k);
        if (S.paused) await post(xr("/press"), { hand: btn[0], button: btn[1], frames: 6 });
        else await post(xr("/hold"), { hand: btn[0], button: btn[1], down: true });
      }
    } catch (err) { fail(err); }
  });
  window.addEventListener("keyup", (e) => {
    const k = e.key.toLowerCase();
    if (!held.has(k)) return;
    held.delete(k);
    const btn = { " ": ["right", "trigger"], g: ["right", "grip"], "1": ["right", "a"], "2": ["right", "b"],
      "3": ["left", "x"], "4": ["left", "y"], m: ["left", "menu"] }[k];
    if (btn && S.current && !S.paused) post(xr("/hold"), { hand: btn[0], button: btn[1], down: false }).catch(() => {});
  });

  /* ---------------- status, feed, controls ---------------- */
  async function poll() {
    if (!S.current || !active()) return;
    try {
      const st = await api(xr("/state"));
      S.paused = !!st.paused; renderPaused();
      const stats = st.stats || {};
      S.refresh = stats.refresh_hz || S.refresh;
      $("#sim-state").textContent = (st.session || "—").toUpperCase();
      $("#sim-chip").className = "chip " + (st.session === "focused" ? "on" : "warn");
      $("#sim-frame").textContent = `frame ${st.render_frame}`;
      $("#sim-fps").textContent = stats.fps ? `${stats.fps.toFixed(0)} / ${S.refresh.toFixed(0)} fps · p99 ${stats.frame_ms_p99.toFixed(1)} ms` : "";
      const head = st.head || {};
      $("#sim-kv").innerHTML = [
        ["head", head.ypr ? `yaw ${head.ypr[0].toFixed(0)}° · pitch ${head.ypr[1].toFixed(0)}°` : "—"],
        ["position", head.pos ? head.pos.map(v => v.toFixed(2)).join(", ") + " m" : "—"],
        ["missed", stats.missed_frames != null ? `${stats.missed_frames} frames` : "—"],
        ["cpu / frame", stats.cpu_ms_p50 != null ? `${stats.cpu_ms_p50.toFixed(1)} ms` : "—"],
      ].map(([k, v]) => `<dt>${k}</dt><dd>${esc(v)}</dd>`).join("");
      if (!S.paused && stats.fps) { S.fpsHistory.push(stats.fps); if (S.fpsHistory.length > 90) S.fpsHistory.shift(); spark(); }
      const ev = await api(`${xr("/events")}?since=${S.events}`);
      if (ev.events?.length) {
        const feed = $("#sim-feed");
        feed.querySelector(".empty")?.remove();
        for (const e of ev.events) {
          const li = document.createElement("li");
          let ic = "•", cls = "", text = e.type;
          if (e.type === "haptic") { ic = "≋"; cls = "haptic"; text = `haptic ${e.hand} · ${e.action || "?"} · amp ${(+e.amplitude || 0).toFixed(2)}`; }
          else if (e.type === "session_state") { ic = "◆"; cls = "state"; text = `session ${e.state}${e.synthetic ? " (simulated)" : ""}`; }
          else if (e.type === "interaction_profile") { ic = "⌁"; text = `controllers: ${(e.profile || "none").split("/").pop()}`; }
          else if (e.type === "injected") { ic = "↯"; text = `you sent ${e.event}`; }
          li.innerHTML = `<span class="ic ${cls}">${ic}</span><span>${esc(text)}</span><time>${clock()}</time>`;
          feed.prepend(li);
        }
        while (feed.children.length > 80) feed.lastChild.remove();
        S.events = ev.latest;
      }
    } catch (e) { /* the tab bar shows when an app goes away */ }
  }
  function spark() {
    const c = $("#sim-spark"), g = c.getContext("2d"), W = c.width, H = c.height;
    g.clearRect(0, 0, W, H);
    const top = S.refresh * 1.1;
    g.strokeStyle = "rgba(201,133,0,.6)"; g.setLineDash([6, 6]); g.lineWidth = 2;
    const ry = H - (S.refresh / top) * H; g.beginPath(); g.moveTo(0, ry); g.lineTo(W, ry); g.stroke();
    g.setLineDash([]);
    const n = S.fpsHistory.length; if (n < 2) return;
    g.strokeStyle = "#6bcbdf"; g.lineWidth = 3; g.beginPath();
    S.fpsHistory.forEach((v, i) => { const x = (i / 89) * W, y = H - Math.min(v, top) / top * H; i ? g.lineTo(x, y) : g.moveTo(x, y); });
    g.stroke();
  }
  async function loadInfo() {
    if (!S.current) return;
    try {
      const info = await api(xr("/info"));
      $("#sim-app-name").textContent = info.app ? `${info.app.name} · ${info.app.engine || ""}` : "";
      const acts = (info.actions || []).filter(a => a.bindings.length && a.type !== "pose" && a.type !== "haptic");
      $("#sim-act-count").textContent = acts.length ? `${acts.length} bound` : "";
      $("#sim-actions").innerHTML = acts.slice(0, 80).map(a => {
        const on = a.state && a.state.active && (a.state.value === true || (typeof a.state.value === "number" && a.state.value > 0.05)
          || (Array.isArray(a.state.value) && Math.hypot(...a.state.value) > 0.05));
        const b = a.bindings[0];
        return `<div class="act ${on ? "on" : ""}" title="${esc(a.name)}"><span class="led"></span><span class="nm">${esc(a.name)}</span>
          <span class="bd">${esc(b.hand[0].toUpperCase() + " " + b.input + (b.component ? "/" + b.component : ""))}</span></div>`;
      }).join("") || '<div class="empty">The app bound no controller actions.</div>';
    } catch {}
  }

  /* ---------------- playtests ---------------- */
  async function loadPersonas() {
    try { S.personas = await api("/api/xr/personas"); } catch { return; }
    const box = $("#pt-personas");
    box.innerHTML = S.personas.map(p => `<button class="persona" role="radio" aria-checked="${p.key === S.persona}" data-k="${esc(p.key)}">
      <b>${esc(p.name)}</b><span>${esc(p.tagline)}</span></button>`).join("");
    box.querySelectorAll(".persona").forEach(b => b.onclick = () => {
      S.persona = b.dataset.k;
      box.querySelectorAll(".persona").forEach(x => x.setAttribute("aria-checked", x === b ? "true" : "false"));
    });
  }
  function updateCost() {
    const steps = +$("#pt-steps").value || 30;
    const claude = $("#pt-brain").value === "claude";
    $("#pt-cost").textContent = claude
      ? `≈ $${(steps * 0.012).toFixed(2)}–$${(steps * 0.03).toFixed(2)} of Claude Opus 5.5`
      : "free · reproducible from its seed";
    $("#pt-key").hidden = !claude;
    $("#pt-go").disabled = claude && !S.claude?.ready;
  }
  function showClaude(c) {
    S.claude = c;
    const ok = !!c?.ready;
    $("#pt-key-in").hidden = ok; $("#pt-key-ok").hidden = !ok;
    $("#pt-key").classList.toggle("env", c?.kind === "env" || c?.kind === "profile");
    $("#pt-key-label").textContent = ok ? c.label : "";
    updateCost();
  }
  async function loadClaude() {
    try { showClaude(await api("/api/xr/claude")); } catch { showClaude(null); }
  }
  $("#pt-key-form").onsubmit = async (ev) => {
    ev.preventDefault();
    const field = $("#pt-key-field"), form = $("#pt-key-form"), key = field.value.trim();
    if (!key) { field.setAttribute("aria-invalid", "true"); field.focus(); return; }
    form.classList.add("busy"); $("#pt-key-save").textContent = "CHECKING…";
    try {
      const r = await post("/api/xr/claude", { key });
      field.value = ""; field.removeAttribute("aria-invalid");
      showClaude(r);
      toast(r.note ? `Key saved — ${r.note}` : "Key checked and saved");
    } catch (e) { field.setAttribute("aria-invalid", "true"); fail(e); }
    finally { form.classList.remove("busy"); $("#pt-key-save").textContent = "SAVE KEY"; }
  };
  $("#pt-key-field").oninput = () => $("#pt-key-field").removeAttribute("aria-invalid");
  $("#pt-key-forget").onclick = async () => {
    try { showClaude(await api("/api/xr/claude", { method: "DELETE" })); toast("Key forgotten"); } catch (e) { fail(e); }
  };
  $("#pt-steps").oninput = updateCost; $("#pt-brain").onchange = updateCost;
  $("#pt-go").onclick = async () => {
    const target = $("#pt-target").value.trim() || $("#pt-target").placeholder;
    if (!target) return toast("Pick an app to playtest.", true);
    try {
      const r = await post("/api/xr/playtest", { target, persona: S.persona, goal: $("#pt-goal").value.trim() || null,
        brain: $("#pt-brain").value, steps: +$("#pt-steps").value || 30 });
      S.playtest = { name: r.name, since: 0, steps: +$("#pt-steps").value || 30 };
      $("#pt-live").hidden = false; $("#pt-live-feed").innerHTML = ""; $("#pt-live-thought").textContent = "";
      $("#pt-live-img").removeAttribute("src");
      const persona = S.personas.find(p => p.key === S.persona);
      $("#pt-live-title").textContent = `${persona ? persona.name : S.persona} · ${target.split("/").pop()}`;
      toast("Playtest started — it boots the app first");
    } catch (e) { fail(e); }
  };
  async function pollPlaytest() {
    const pt = S.playtest;
    if (!pt) return;
    try {
      const r = await api(`/api/xr/playtest/${encodeURIComponent(pt.name)}?since=${pt.since}`);
      $("#pt-live-state").textContent = (r.status || "").toUpperCase();
      $("#pt-live-chip").className = "chip " + (r.status === "done" ? "on" : r.status === "playing" ? "on" : "warn");
      $("#pt-live-bar").style.width = `${Math.min(100, (r.step / pt.steps) * 100)}%`;
      for (const e of r.events) {
        pt.since = Math.max(pt.since, e.t);
        if (e.kind === "view" && e.screenshot) $("#pt-live-img").src = `/api/xr/runs/${encodeURIComponent(pt.name)}/${e.screenshot}?t=${e.t}`;
        if (e.kind === "action" && e.thought) $("#pt-live-thought").textContent = `“${e.thought.slice(0, 220)}”`;
        if (e.kind === "status" && e.message) $("#pt-live-thought").textContent = e.message;
        if (e.kind === "action" || e.kind === "issue") {
          const li = document.createElement("li");
          if (e.kind === "action") li.innerHTML = `<span class="ic">${e.step}</span><span>${esc(e.action)} <span style="color:var(--faint)">${esc(JSON.stringify(e.args))}</span></span><time></time>`;
          else li.innerHTML = `<span class="ic issue">!</span><span><b>${esc(e.issue.severity)}</b> ${esc(e.issue.title)}</span><time></time>`;
          $("#pt-live-feed").prepend(li);
        }
      }
      if (["done", "crashed", "failed"].includes(r.status)) {
        S.playtest = null;
        $("#pt-live-thought").textContent = r.verdict?.summary || r.note || "";
        toast(`Playtest ${r.status}`, r.status !== "done");
        loadReports();
      }
    } catch (e) { S.playtest = null; }
  }
  async function loadReports() {
    let runs = [];
    try { runs = await api("/api/xr/runs"); } catch {}
    const box = $("#pt-reports");
    if (!runs.length) { box.innerHTML = '<div class="empty">No playtests yet. Start one above — or run <code>visor sim playtest demo --brain monkey</code>.</div>'; return; }
    box.innerHTML = runs.map(r => {
      if (r.kind === "fleet") {
        return `<a class="report" href="${esc(r.report)}" target="_blank"><div class="th" style="background:linear-gradient(135deg,#1d232d,#101318)"><span class="kind">fleet</span>
          <span class="score">${r.ok}/${r.count}</span></div><div class="bd"><b>${esc(String(r.app).split("/").pop())}</b>
          <span class="sub">${r.count} copies · ${r.mean_fps} fps mean · ${r.crashed} crashed</span>
          <div class="sev"><span>${r.issues} distinct issue${r.issues === 1 ? "" : "s"}</span></div></div></a>`;
      }
      if (r.live) {
        return `<div class="report"><div class="th"><span class="kind">playing</span></div><div class="bd"><b>${esc(r.app)}</b>
          <span class="sub">${esc(r.persona)} · step ${r.step} · ${r.issues_live} issues so far</span></div></div>`;
      }
      const i = r.issues || {};
      const chips = ["blocker", "major", "minor", "note"].filter(s => i[s]).map(s => `<span class="${s}">${i[s]} ${s}</span>`).join("")
        || '<span class="ok">no issues</span>';
      const score = r.verdict && r.verdict.rating != null ? `<span class="score">${r.verdict.rating}/10</span>` : "";
      return `<a class="report" href="${esc(r.report)}" target="_blank">
        <div class="th" style="${r.thumb ? `background-image:url('${esc(r.thumb)}')` : ""}"><span class="kind">${r.crash ? "crashed" : esc(r.brain === "claude" ? "playtest" : "monkey")}</span>${score}</div>
        <div class="bd"><b>${esc(r.app)}</b><span class="sub">${esc(r.persona)} · ${r.steps} actions · ${esc((r.created || "").replace("T", " "))}</span>
        <div class="sev">${chips}</div></div></a>`;
    }).join("");
  }
  $("#fl-go").onclick = async () => {
    const target = $("#fl-target").value.trim() || "demo";
    try {
      const r = await post("/api/xr/fleet", { target, count: +$("#fl-count").value || 4, mode: $("#fl-mode").value });
      $("#fl-note").textContent = `fleet ${r.name} running — it'll appear in Reports when done`;
      const tick = setInterval(async () => {
        try {
          const p = await api(`/api/xr/fleet/${encodeURIComponent(r.name)}`);
          $("#fl-note").textContent = `${p.done}/${p.count} done · ${p.running} running · ${p.failed} failed`;
          if (p.finished) { clearInterval(tick); loadReports(); $("#fl-note").textContent += " — finished"; }
        } catch { clearInterval(tick); }
      }, 2000);
    } catch (e) { fail(e); }
  };

  /* ---------------- voice ---------------- */
  const LANG_NAMES = { en: "English", es: "Spanish", fr: "French", de: "German", it: "Italian", pt: "Portuguese",
    nl: "Dutch", pl: "Polish", ru: "Russian", uk: "Ukrainian", tr: "Turkish", sv: "Swedish", ar: "Arabic",
    hi: "Hindi", sw: "Swahili", vi: "Vietnamese", zh: "Chinese" };
  S.volume = 1; S.saidCount = -1; S.voicesReady = false;
  async function loadVoices() {
    try {
      const v = await api("/api/xr/voices");
      const sel = $("#sim-say-lang");
      sel.innerHTML = Object.keys(v.languages).sort((a, b) => (a === "en" ? -1 : b === "en" ? 1 : a.localeCompare(b)))
        .map(code => `<option value="${code}">${esc(LANG_NAMES[code] || code)}${v.installed.includes(v.languages[code]) ? "" : " ↓"}</option>`)
        .join("");
      sel.title = "Language — ↓ downloads its voice (~60 MB) the first time";
      S.voicesReady = v.available;
      if (!v.available) {
        $("#sim-said").innerHTML = '<li class="empty">Speaking needs a text-to-speech engine: <code>pip install \'visor-dev[voice]\'</code></li>';
        $("#sim-voice").classList.add("off");
      }
    } catch (e) { /* the card stays as it is */ }
  }
  function micLabel(st) {
    const el = $("#sim-micstate");
    if (!st || !st.available) { el.className = "micstate"; el.querySelector("b").textContent = "REAL MIC"; el.title = st?.reason || ""; return; }
    const speaking = st.speaking && st.world !== "paused";
    el.className = "micstate" + (speaking ? " speaking" : st.listening ? " on" : "");
    el.querySelector("b").textContent = speaking ? "SPEAKING" : st.speaking ? "WAITING FOR THE WORLD" : st.listening ? "LISTENING" : "NOT LISTENING";
    el.title = st.listening ? `the app has its microphone open (${st.streams.map(s => `${s.rate} Hz × ${s.channels}`).join(", ")})`
      : "the app hasn't opened its microphone, so it can't hear you right now";
  }
  // The conversation: what you said (from the mic's history) and what the app said back
  // (written out by the server's ears), in time order.
  S.mine = []; S.theirs = []; S.heardSeq = 0;
  const clock2 = (at) => new Date(at * 1000).toLocaleTimeString([], { hour12: false });
  function renderSaid() {
    const rows = [
      ...S.mine.map(h => ({ at: h.at, html: h.kind === "speech"
        ? `<li class="you" data-text="${esc(h.label)}" title="Say it again"><q>${esc(h.label)}</q><small>${h.duration.toFixed(1)}s · ${clock2(h.at)}</small></li>`
        : `<li class="you rec"><span>♪ ${esc(h.label || "recording")}</span><small>${h.duration.toFixed(1)}s · ${clock2(h.at)}</small></li>` })),
      ...S.theirs.map(e => {
        const lang = e.language && e.language !== "en" ? `<span class="lang">${esc(e.language)}</span>` : "";
        const ambient = !e.said;
        const body = e.said ? `“${esc(e.said)}”${lang}` : esc(e.text);
        const clip = `<button type="button" class="replay" data-seq="${e.seq}" title="Hear it again" aria-label="Hear it again">▶</button>`;
        return { at: e.at, html: `<li class="app${ambient ? " ambient" : ""}"><span class="ic">🔊</span><span class="txt">${body}</span>`
          + `<span class="end"><small>${clock2(e.at)}</small>${clip}</span></li>` };
      }),
    ].sort((a, b) => b.at - a.at);
    if (!rows.length) return;
    $("#sim-said").innerHTML = rows.slice(0, 60).map(r => r.html).join("");
  }
  async function pollMic() {
    if (!S.current || !active()) return;
    try {
      const st = await api(xr("/mic"));
      micLabel(st);
      speakerLabel(st.speakers);
      $("#sim-voice").classList.toggle("off", !st.available || !S.voicesReady);
      if (st.available && st.history.length !== S.saidCount) { S.saidCount = st.history.length; S.mine = st.history; renderSaid(); }
      if (!st.available) $("#sim-said").innerHTML = `<li class="empty">${esc(st.reason)}</li>`;
    } catch (e) { /* app gone: the tab bar handles it */ }
  }
  async function pollHeard() {
    if (!S.current || !active()) return;
    const name = S.current;
    try {
      const r = await api(`${xr("/heard")}?since=${S.heardSeq}`);
      if (name !== S.current) return;
      if (r.entries?.length) {
        S.theirs = [...S.theirs, ...r.entries].slice(-40);
        S.heardSeq = r.entries[r.entries.length - 1].seq;
        renderSaid();
      }
      if (r.speakers) speakerLabel(r.speakers);
    } catch (e) { /* the next poll will tell */ }
  }
  function speakerLabel(sp) {
    const b = $("#sim-speaker");
    if (!sp) return;
    const db = sp.playing ? sp.level_db : -120;
    const share = Math.max(0, Math.min(1, (db + 60) / 50));
    b.classList.toggle("playing", !!sp.playing);
    b.classList.toggle("loud", db > -24);
    $("#sim-meter i").style.width = `${Math.round(share * 100)}%`;
    const listening = b.getAttribute("aria-pressed") === "true";
    b.title = (sp.open ? (sp.playing ? `playing sound (${Math.round(db)} dB)` : "speakers open, quiet") : "no sound yet")
      + (listening ? " — click to stop hearing it here" : " — click to hear it here (it plays nowhere else)");
  }
  $("#sim-said").addEventListener("click", (e) => {
    const b = e.target.closest(".replay");
    if (!b) return;
    e.stopPropagation();
    new Audio(`${xr("/heard")}/${b.dataset.seq}.wav`).play().catch(() => toast("That sound is no longer kept."));
  });

  // Hearing the app live: raw 16 kHz PCM streamed from the server, scheduled gap-free with Web Audio.
  S.live = null;
  async function listenLive(on) {
    const b = $("#sim-speaker");
    if (S.live) { S.live.abort.abort(); S.live.ctx.close(); S.live = null; }
    b.setAttribute("aria-pressed", String(on));
    if (!on || !S.current) return;
    const ctx = new AudioContext();
    const abort = new AbortController();
    S.live = { ctx, abort };
    let at = 0, carry = new Uint8Array(0);
    try {
      const res = await fetch(xr("/listen"), { signal: abort.signal });
      const reader = res.body.getReader();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        const bytes = new Uint8Array(carry.length + value.length);
        bytes.set(carry); bytes.set(value, carry.length);
        const n = bytes.length >> 1;
        carry = bytes.slice(n * 2);
        if (!n) continue;
        const pcm = new Int16Array(bytes.buffer.slice(0, n * 2));
        const buf = ctx.createBuffer(1, n, 16000);
        const ch = buf.getChannelData(0);
        for (let i = 0; i < n; i++) ch[i] = pcm[i] / 32768;
        const src = ctx.createBufferSource();
        src.buffer = buf; src.connect(ctx.destination);
        at = Math.max(at, ctx.currentTime + 0.15);  // a little latency buys a gap-free stream
        src.start(at); at += buf.duration;
      }
    } catch (e) { if (e.name !== "AbortError") toast("Lost the app's sound."); }
    if (S.live && S.live.abort === abort) { b.setAttribute("aria-pressed", "false"); S.live = null; ctx.close(); }
  }
  $("#sim-speaker").addEventListener("click", () => listenLive($("#sim-speaker").getAttribute("aria-pressed") !== "true"));

  async function sayIt(text) {
    text = (text || "").trim();
    if (!text || !S.current) return;
    const go = $("#sim-say-go");
    go.disabled = true; go.textContent = "…";
    try {
      const r = await post(xr("/say"), { text, language: $("#sim-say-lang").value || null, volume: S.volume });
      $("#sim-say").value = "";
      if (!r.listening) toast("Said — but the app isn't listening right now, so it heard nothing.");
      pollMic();
    } catch (e) { fail(e); }
    finally { go.disabled = false; go.textContent = "SAY"; }
  }
  $("#sim-say-form").addEventListener("submit", (e) => { e.preventDefault(); sayIt($("#sim-say").value); });
  $("#sim-said").addEventListener("click", (e) => { const li = e.target.closest("li[data-text]"); if (li) sayIt(li.dataset.text); });
  $("#sim-say-vol").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-vol]"); if (!b) return;
    S.volume = +b.dataset.vol;
    $("#sim-say-vol").querySelectorAll("button").forEach(x => x.setAttribute("aria-checked", String(x === b)));
  });

  /* ---------------- wiring ---------------- */
  $("#sim-new").onclick = () => openLaunch($("#sim-launch").hidden);
  $("#sim-open-launch").onclick = () => openLaunch(true);
  $("#sl-cancel").onclick = () => openLaunch(false);
  $("#sl-go").onclick = () => launch();
  $("#sl-target").addEventListener("keydown", (e) => { if (e.key === "Enter") launch(); });
  $("#sim-try-demo").onclick = () => { $("#sl-target").value = "demo"; launch("demo"); };

  let started = false;
  function onRoute() {
    const id = location.hash.slice(1);
    if (id === "sim" || id === "playtests") {
      if (!started) { started = true; loadSuggest(); loadPersonas(); updateCost(); loadVoices(); }
      if (id === "sim") { refreshApps(); if (S.current && S.streamKey !== S.current) startStream(); }
      if (id === "playtests") { loadReports(); loadClaude(); }
    } else if (S.streamKey) {
      stopStream();  // don't keep a stream running in the background
      listenLive(false);
    }
  }
  window.addEventListener("hashchange", onRoute);
  onRoute();
  setInterval(() => { if (active()) refreshApps(); }, 2500);
  setInterval(poll, 700);
  setInterval(() => { if (active()) loadInfo(); }, 1200);
  setInterval(pollMic, 900);
  setInterval(pollHeard, 1200);
  setInterval(pollPlaytest, 1000);
})();
