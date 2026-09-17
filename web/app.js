"use strict";

// --- tiny helpers -----------------------------------------------------------

const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => [...r.querySelectorAll(s)];

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") n.className = v;
    else if (k === "html") n.innerHTML = v;
    else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) n.setAttribute(k, v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    n.append(kid.nodeType ? kid : document.createTextNode(kid));
  }
  return n;
}

// Live MJPEG streams need care: every <img> must carry its own connkey (two
// streams on one connkey kill each other) and a dropped stream must have its
// src cleared, or the nph-zms process behind it lives on and starves the
// browser's ~6-connections-per-origin budget.
const FPS = { 30: 5, 50: 10, 100: 15 };   // quality scale -> maxfps
function streamSrc(url, scale) {
  return url.replace(/connkey=\d+/, "connkey=" + Math.floor(100000 + Math.random() * 900000))
            .replace(/scale=\d+/, "scale=" + scale)
            .replace(/maxfps=\d+/, "maxfps=" + (FPS[scale] || 10));
}
function killStreams(node) {
  $$("img", node).forEach((i) => i.removeAttribute("src"));
  $$("video", node).forEach((v) => { v.pause(); v.removeAttribute("src"); v.load(); });
}

async function get(path, params = {}) {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== "" && v !== undefined));
  const r = await fetch("/api/" + path + (q.toString() ? "?" + q : ""));
  const j = await r.json();
  if (!r.ok || j?.error) throw new Error(j?.error || "Request failed (" + r.status + ")");
  return j;
}

async function post(path, body = {}) {
  const r = await fetch("/api/" + path, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  });
  const j = await r.json();
  if (!r.ok || j?.error || j?.ok === false) throw new Error(j?.error || j?.out || "Changes were not saved.");
  return j;
}

let toastTimer;
function toast(msg, bad = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("bad", bad);
  t.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 4000);
}

const fail = (e) => toast(e.message || String(e), true);
const gb = (n) => (n / 1073741824).toFixed(0);

let modalOrigin;
let modalBusy = false;
function modal(...content) {
  const body = $("#modal-body");
  if ($("#modal").classList.contains("hidden")) modalOrigin = document.activeElement;
  killStreams(body);
  body.className = "sheet";
  $("#modal").dataset.dirty = "";
  body.replaceChildren(...content);
  $("#modal").classList.remove("hidden");
  requestAnimationFrame(() => ($("input, select, button", body) || $("#modal")).focus());
  return body;
}
function closeModal() {
  const shell = $("#modal");
  if (modalBusy) return;
  if (shell.dataset.dirty && !confirm("Discard unsaved changes?")) return;
  shell.dataset.dirty = "";
  shell.classList.add("hidden");
  killStreams($("#modal-body"));
  $("#modal-body").replaceChildren();
  modalOrigin?.focus();
  return true;
}
$("#modal").addEventListener("click", (e) => { if (e.target.id === "modal") closeModal(); });

let viewerOrigin;
function viewer(...content) {
  const v = $("#viewer");
  viewerOrigin = document.activeElement;
  killStreams(v);
  v.replaceChildren(...content);
  v.classList.remove("hidden");
  ($("button", v) || v).focus();
}
function closeViewer() { const v = $("#viewer"); v.classList.add("hidden"); killStreams(v); v.replaceChildren(); viewerOrigin?.focus(); }
$("#viewer").addEventListener("click", (e) => { if (e.target.id === "viewer") closeViewer(); });
addEventListener("keydown", (e) => {
  const active = [$("#viewer"), $("#modal")].find((n) => !n.classList.contains("hidden"));
  if (!active) return;
  if (e.key === "Escape") { active.id === "viewer" ? closeViewer() : closeModal(); }
  if (e.key === "Tab") {
    const controls = $$("button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex='0']", active).filter((n) => n.getClientRects().length);
    const first = controls[0] || active, last = controls.at(-1) || active;
    if (!active.contains(document.activeElement) || (e.shiftKey && document.activeElement === first)) { e.preventDefault(); (e.shiftKey ? last : first).focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});
addEventListener("beforeunload", (e) => {
  if ($("#modal").dataset.dirty || Object.keys(configDraft).length) { e.preventDefault(); e.returnValue = ""; }
});

// --- shared state -----------------------------------------------------------

const state = { cameras: [], modes: [], zoneTypes: [], page: "cameras" };

const modeLabel = (v) => (state.modes.find((m) => m.value === v) || {}).label || v;

function modeSelect(value, onchange) {
  return el("select", { onchange },
    state.modes.map((m) => el("option", { value: m.value, selected: m.value === value }, m.label)));
}

// --- navigation -------------------------------------------------------------

const PAGES = {};

$$("#topnav a").forEach((a) => {
  a.href = "#" + a.dataset.page;
  a.setAttribute("aria-label", [...a.childNodes].filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join("").trim());
  $(".nav-icon", a)?.setAttribute("aria-hidden", "true");
});
addEventListener("hashchange", () => show(location.hash.slice(1) || "cameras"));

function show(name) {
  if (!PAGES[name]) name = "cameras";
  if (!$("#modal").classList.contains("hidden") && !closeModal()) {
    history.replaceState(null, "", "#" + state.page); return;
  }
  state.page = name;
  if (location.hash.slice(1) !== name) location.hash = name;
  $$("#topnav a").forEach((a) => { a.classList.toggle("active", a.dataset.page === name); a.setAttribute("aria-current", a.dataset.page === name ? "page" : "false"); });
  $$(".page").forEach((p) => p.classList.toggle("hidden", p.id !== "page-" + name));
  stopLive(name !== "live");
  if (PAGES[name]) PAGES[name]().catch(fail);
}

// --- theme ------------------------------------------------------------------
// The starting theme is set by an inline script in index.html, before the
// stylesheet loads, so the page never flashes the wrong colours.

function paintTheme() {
  $("#theme-btn").textContent = document.documentElement.dataset.theme === "dark" ? "☀  Light appearance" : "☾  Dark appearance";
}

$("#theme-btn").addEventListener("click", () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.theme = next;
  paintTheme();
});
paintTheme();

// --- health banner ----------------------------------------------------------

async function refreshHealth() {
  const h = $("#health");
  try {
    const s = await get("status");
    if (s.ok) {
      h.className = "health ok";
      h.textContent = "Cameras running";
      h.title = "ZoneMinder " + s.version;
    } else {
      h.className = "health bad";
      h.replaceChildren("Not responding. ",
        el("button", { class: "link", onclick: restart }, "Fix it"));
    }
    return s;
  } catch (e) {
    h.className = "health bad";
    h.replaceChildren("Can't reach the camera system. ",
      el("button", { class: "link", onclick: restart }, "Fix it"));
    return { ok: false };
  }
}

async function restart() {
  toast("Restarting. This takes a few seconds...");
  try {
    await post("restart");
    setTimeout(() => { refreshHealth(); show(state.page); }, 6000);
  } catch (e) { fail(e); }
}

// --- page: cameras ----------------------------------------------------------

$("#btn-live").addEventListener("click", () => { location.hash = "live"; });

PAGES.cameras = async function () {
  const grid = $("#camera-grid");
  if (!state.cameras.length) grid.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  let cams, status;
  try { [cams, status] = await Promise.all([get("cameras"), get("status")]); }
  catch (e) {
    grid.replaceChildren(el("div", { class: "card pad" }, el("h2", {}, "Camera system unavailable"), el("p", { class: "hint" }, "Check the connection in Settings → Diagnostics, then try again."), el("button", { onclick: () => PAGES.cameras().catch(fail) }, "Try again")));
    $("#recent-activity")?.replaceChildren();
    return;
  }
  state.cameras = cams;
  $("#camera-count").textContent = cams.filter((c) => c.status === "ok").length + " of " + cams.length + " online";
  drawTiles(status);
  drawSnooze(status);
  if (!cams.length) {
    grid.replaceChildren(welcomeCard());
    $("#recent-activity").replaceChildren(el("p", { class: "hint" }, "Add a camera to start recording activity."));
    return;
  }
  grid.replaceChildren(...cams.map(cameraCard));
  try {
    const recent = await get("events", { limit: 4 });
    $("#recent-activity").replaceChildren(...(recent.events.length ? recent.events.map((e) => {
      const camera = cams.find((c) => String(c.id) === String(e.monitor));
      return el("button", { class: "activity-row", onclick: () => playEvent(e) }, el("img", { src: e.thumb, alt: "", loading: "lazy" }), el("span", {}, el("strong", {}, camera?.name || "Camera"), el("small", {}, e.person ? "Person detected" : e.animal ? "Animal detected" : "Movement recorded")), el("time", {}, e.start), el("span", { "aria-hidden": "true" }, "↗"));
    }) : [el("p", { class: "hint" }, "No recordings yet. New activity will appear here.")]));
  } catch { $("#recent-activity").textContent = "Recent activity is unavailable."; }
  cameraTimer();
};

// Previews are stills, so refresh them the way Ring and Frigate do: about once a
// minute, and never while the user has something open on top of the page.
function cameraTimer() {
  clearInterval(state.cameraTimer);
  state.cameraTimer = setInterval(() => {
    if (state.page !== "cameras") return clearInterval(state.cameraTimer);
    if (!$("#modal").classList.contains("hidden") || !$("#viewer").classList.contains("hidden")
        || $("details.menu[open]")) return;
    PAGES.cameras().catch(() => {});
  }, 60000);
}

// Nothing to look at on a fresh install, so give first-timers something to press
// even if their camera is still in its box.
function welcomeCard() {
  return el("div", { class: "card pad welcome" },
    el("h2", {}, "Welcome"),
    el("p", { class: "muted" },
      "Add a camera and Porchlight finds it on your network and fills in the technical parts. "
      + "No camera to hand? Try the app with a sample video first."),
    el("div", { class: "row" },
      el("button", { class: "primary", onclick: () => $("#btn-add").click() }, "Add a camera"),
      el("button", { onclick: async (e) => {
        e.target.disabled = true;
        toast("Making a sample video. This takes a few seconds...");
        try { await post("camera/sample"); toast("Sample camera added."); PAGES.cameras(); }
        catch (err) { e.target.disabled = false; fail(err); }
      } }, "Try it with a sample video")));
}

function ago(t) {
  if (!t) return "No recordings yet";
  const s = Date.now() / 1000 - t;
  if (s < 90) return "Just now";
  if (s < 3600) return Math.round(s / 60) + "m ago";
  if (s < 86400) return Math.round(s / 3600) + "h ago";
  return Math.round(s / 86400) + "d ago";
}

function cameraCard(c) {
  const stamp = el("span", { class: "snapshot-time" }, "Loading snapshot…");
  const badge = c.status === "ok" ? el("span", { class: "badge live" }, "Online")
    : c.status === "offline" ? el("span", { class: "badge bad" }, "Offline")
    : el("span", { class: "badge" }, "Off · " + ago(c.last));
  // A stopped monitor answers the snapshot URL with no body, so don't ask for one.
  const preview = c.status === "ok"
    ? el("div", { class: "preview" },
        el("img", { class: "thumb", src: c.snapshot, alt: c.name, loading: "lazy",
          tabindex: "0", role: "button", "aria-label": "Watch " + c.name,
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); watch(c); } },
          onload: () => { stamp.textContent = "Snapshot · " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); },
          onerror: () => { stamp.textContent = "Snapshot unavailable"; },
          onclick: () => watch(c) }), badge, stamp)
    : el("div", { class: "preview off" },
        c.status === "offline" ? "Can't reach this camera." : "This camera is turned off.", badge);

  const menu = el("details", { class: "menu" }, el("summary", {}, "⋯"),
    el("div", { class: "pop" },
      el("button", { onclick: () => watch(c) }, "Watch"),
      el("button", { onclick: () => zoneEditor(c.id) }, "Areas"),
      c.controllable && el("button", { onclick: () => ptzPad(c) }, "Move"),
      el("button", { class: "danger", onclick: () => removeCamera(c) }, "Remove")));

  return el("div", { class: "card" },
    el("div", { class: "head" },
      el("span", { class: "dot " + (c.status === "ok" ? "on" : c.status === "offline" ? "off" : "") }),
      el("span", {}, c.name),
      el("span", { class: "spacer" }),
      el("button", { class: "gear", title: "Settings", "aria-label": "Settings for " + c.name, onclick: () => cameraSettings(c.id).catch(fail) }, "Settings ↗")),
    preview,
    el("div", { class: "foot" },
      modeSelect(c.function, async (e) => {
        try { await post("camera/mode", { id: c.id, function: e.target.value }); toast("Saved."); }
        catch (err) { fail(err); }
      }), menu));
}

// One listener closes whichever card menu is open when you click elsewhere.
document.addEventListener("click", (e) => {
  $$("details.menu[open]").forEach((d) => { if (!d.contains(e.target)) d.open = false; });
});

function drawTiles(s) {
  const tile = (onclick, label, ...body) =>
    el("div", { class: "card pad tile", role: "button", tabindex: "0", onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); onclick(); } }, onclick },
      el("div", { class: "muted" }, label), ...body);
  const gb = (n) => (n / 1e9).toFixed(0) + " GB";
  const st = s.storage || {};
  const pct = st.total ? Math.round(100 * st.used / st.total) : 0;
  $("#tiles").replaceChildren(
    tile(() => { location.hash = "modes"; }, "Home / Away",
      el("div", { class: "big" }, s.state || "—")),
    tile(() => { selectSettings("storage"); location.hash = "system"; }, "Storage",
      el("div", { class: "bar-track" },
        el("div", { class: "bar-fill",
          style: "width:" + pct + "%" + (pct >= 90 ? ";background:var(--bad)" : "") })),
      el("div", {}, st.total ? gb(st.free) + " free of " + gb(st.total)
        + (pct >= 90 ? " — running low" : "") : "—")),
    tile(() => { location.hash = "recordings"; }, "Recordings today",
      el("div", { class: "big" }, s.today == null ? "—" : String(s.today))),
    tile(() => { selectSettings("diagnostics"); location.hash = "system"; }, "System health",
      s.ok ? el("div", {}, "ZoneMinder " + s.version)
           : el("div", {}, "Not running ",
               el("button", { class: "link", onclick: (e) => { e.stopPropagation(); restart(); } }, "Fix it"))));
}

function drawSnooze(s) {
  const bar = $("#snooze-bar");
  bar.classList.remove("hidden");
  const set = (m) => async () => {
    try { await post("snooze", { minutes: m }); PAGES.cameras(); } catch (e) { fail(e); }
  };
  if (s.snooze_until * 1000 > Date.now()) {
    const t = new Date(s.snooze_until * 1000);
    bar.replaceChildren(
      el("span", { class: "muted" },
        "Phone alerts are snoozed until " + t.toTimeString().slice(0, 5) + "."),
      el("button", { onclick: set(0) }, "Resume alerts"));
  } else {
    bar.replaceChildren(
      el("span", { class: "muted" }, "Snooze phone alerts:"),
      ...[["30 min", 30], ["1 hour", 60], ["8 hours", 480]].map(([label, m]) =>
        el("button", { onclick: set(m) }, label)));
  }
}

function watch(c) {
  const img = el("img", { src: streamSrc(c.stream, 100), alt: c.name });
  viewer(img, el("div", { class: "bar" },
    el("span", { class: "muted", style: "color:#ddd" }, c.name),
    c.controllable && el("button", { onclick: () => ptzPad(c) }, "Move camera"),
    el("button", { onclick: closeViewer }, "Close")));
}

async function removeCamera(c) {
  if (!confirm("Remove “" + c.name + "”?\n\nIts recordings will be deleted too.")) return;
  try { await post("camera/delete", { id: c.id }); toast("Camera removed."); PAGES.cameras(); }
  catch (e) { fail(e); }
}

// --- add camera wizard ------------------------------------------------------

$("#btn-add").addEventListener("click", wizardFind);

function wizardFind() {
  const list = el("div", { class: "wizard-list" }, el("div", { class: "muted" }, "Looking for cameras..."));
  let picked = null;
  const next = el("button", { class: "primary", disabled: true, onclick: () => wizardSignIn(picked) }, "Next");

  modal(el("h2", {}, "Add a camera"),
    el("p", { class: "hint" }, "Searching your network for cameras. This takes about half a minute."),
    list,
    el("div", { class: "row" },
      el("button", { class: "link", onclick: wizardManual }, "Type the address instead"),
      el("button", { class: "link", onclick: wizardOther }, "Other kinds of camera"),
      el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"), next));

  post("scan/start").catch(fail);
  const poll = setInterval(async () => {
    let s;
    try { s = await get("scan"); } catch (e) { clearInterval(poll); return; }
    if (!s.done) {
      if (s.stage === "sweep") {
        list.replaceChildren(el("div", { class: "muted" },
          "Nothing announced itself. Knocking on every address on your network..."));
      }
      return;
    }
    clearInterval(poll);
    if (!s.found.length) {
      list.replaceChildren(el("div", { class: "muted" },
        "No cameras found automatically. Use “Type the address instead”."));
      return;
    }
    list.replaceChildren(...s.found.map((f) => el("div", { onclick: (e) => {
      $$(".wizard-list div", list).forEach((d) => d.classList.remove("sel"));
      e.target.classList.add("sel");
      picked = f;
      next.disabled = false;
    } }, f.label)));
  }, 1500);
}

function wizardManual() {
  const host = el("input", { placeholder: "192.168.1.50" });
  modal(el("h2", {}, "Camera address"),
    el("p", { class: "hint" }, "The camera's address on your network — check its app or your router."),
    host,
    el("div", { class: "row" },
      el("button", { class: "link", onclick: wizardFind }, "Back to search"),
      el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: () => {
        if (host.value.trim()) wizardSignIn({ host: host.value.trim() });
      } }, "Next")));
  host.focus();
}

function wizardSignIn(picked) {
  const host = picked.host || (picked.url || "").replace(/^https?:\/\//, "").split(/[:/]/)[0];
  const user = el("input", { value: "admin" });
  const pw = el("input", { type: "password" });
  const msg = el("span", { class: "muted" });
  modal(el("h2", {}, "Sign in to the camera"),
    el("p", { class: "hint" }, "The username and password you set on the camera itself."),
    el("div", { class: "fields" },
      el("label", {}, "Username", user), el("label", {}, "Password", pw)),
    el("div", { class: "row" }, msg, el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: async () => {
        msg.textContent = "Connecting...";
        try {
          const r = await post("probe/profiles",
            { onvif_url: picked.url || "", host, port: picked.port || 554,
              user: user.value, password: pw.value });
          if (r.verified === false) {
            // Adding it anyway just makes a card that says "Offline" for no visible reason.
            msg.style.color = "var(--bad)";
            msg.textContent = r.error;
            return;
          }
          wizardFinish({ kind: "rtsp", path: r.path, host });
        } catch (e) { fail(e); msg.textContent = ""; }
      } }, "Next")));
}

function wizardOther() {
  const kind = el("select", {},
    el("option", { value: "webcam" }, "A webcam plugged into this computer"),
    el("option", { value: "mjpeg" }, "A camera that gives a web picture (http://...)"),
    el("option", { value: "rtsp" }, "A video stream address (rtsp://...)"),
    el("option", { value: "file" }, "A video file on this computer"));
  const path = el("input", { placeholder: "/dev/video0" });
  const list = el("div", { class: "muted" });
  get("webcams").then((w) => {
    list.textContent = w.length ? "Found: " + w.map((x) => x.name + " (" + x.path + ")").join(", ")
                                : "No webcams plugged in.";
    if (w.length) path.value = w[0].path;
  }).catch(() => {});
  modal(el("h2", {}, "Other kinds of camera"),
    el("div", { class: "fields" }, el("label", {}, "Kind", kind), el("label", {}, "Where it is", path)),
    list,
    el("div", { class: "row" },
      el("button", { class: "link", onclick: wizardFind }, "Back to search"),
      el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: () =>
        wizardFinish({ kind: kind.value, path: path.value.trim() }) }, "Next")));
}

function wizardFinish(src) {
  const name = el("input", { value: "Camera" });
  const path = el("input", { value: src.path || "" });
  const mode = modeSelect("Modect");
  modal(el("h2", {}, "Almost done"),
    el("div", { class: "fields" },
      el("label", {}, "Name this camera", name),
      el("label", {}, "What should it do?", mode)),
    el("details", {}, el("summary", {}, "Show advanced"),
      el("label", { class: "muted" }, "Where the video comes from (change only if it doesn't work)"), path),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: async () => {
        try {
          await post("camera/add", { name: name.value.trim() || "Camera", kind: src.kind,
            path: path.value.trim(), function: mode.value });
          closeModal(); toast("Camera added."); PAGES.cameras();
        } catch (e) { fail(e); }
      } }, "Add camera")));
  name.select();
}

// --- camera settings --------------------------------------------------------

const BASIC = ["Name", "Function", "Enabled"];
const CONNECTION = ["Type", "Path", "Device", "Host", "Port", "Method", "User", "Pass"];
const VIDEO = ["Width", "Height", "MaxFPS", "AlarmMaxFPS", "VideoWriter", "RecordAudio", "Colours"];
const MOTION = ["AlarmFrameCount", "PreEventCount", "PostEventCount", "SectionLength",
  "EventPrefix", "AnalysisFPS", "AnalysisFPSLimit", "RefBlendPerc", "AlarmRefBlendPerc", "LinkedMonitors"];
const CONTROL = ["Controllable", "ControlId", "ControlDevice", "ControlAddress"];

const FIELD_LABELS = {
  Name: "Camera name", Function: "Recording mode", Enabled: "Camera enabled",
  Type: "Connection type", Path: "Source path", Device: "Device", Host: "Host",
  Port: "Port", Method: "Connection method", User: "Username", Pass: "Password",
  Width: "Frame width", Height: "Frame height", MaxFPS: "Maximum capture rate",
  AlarmMaxFPS: "Maximum alarm rate", VideoWriter: "Video writer", RecordAudio: "Record audio",
  Colours: "Colour channels", AlarmFrameCount: "Alarm frame count",
  PreEventCount: "Frames before motion", PostEventCount: "Frames after motion",
  SectionLength: "Recording section length", EventPrefix: "Event name prefix",
  AnalysisFPS: "Analysis frame rate", LinkedMonitors: "Linked cameras",
  AnalysisFPSLimit: "Analysis frame rate limit", RefBlendPerc: "Reference image blend", AlarmRefBlendPerc: "Alarm reference blend",
  Controllable: "PTZ enabled", ControlId: "Control profile", ControlDevice: "Control device",
  ControlAddress: "Control address",
};
const FIELD_UNITS = { Width: "px", Height: "px", MaxFPS: "fps", AlarmMaxFPS: "fps", AnalysisFPS: "fps", AnalysisFPSLimit: "fps", AlarmFrameCount: "frames", PreEventCount: "frames", PostEventCount: "frames", SectionLength: "seconds", RefBlendPerc: "%", AlarmRefBlendPerc: "%" };

async function cameraSettings(id) {
  const data = await get("camera", { id });
  if (!state.zoneFields) state.zoneFields = await get("zonefields");
  const m = data.monitor;
  const edited = {};
  let dirty = false;
  const dirtyNote = el("span", { class: "dirty-note" }, "No unsaved changes");
  const apply = el("button", { class: "primary", disabled: true }, "Apply changes");
  const changed = () => {
    dirty = true;
    $("#modal").dataset.dirty = "1";
    dirtyNote.textContent = "Unsaved changes";
    apply.disabled = false;
    if (replay?.children.length) testStatus.textContent = "Settings changed since this replay. Test again to preview the current draft.";
  };
  const label = (k, control) => el("label", {},
    el("span", { class: "field-name" }, FIELD_LABELS[k] || k),
    el("span", { class: "field-key" }, k + (FIELD_UNITS[k] ? " · " + FIELD_UNITS[k] : "")), control);
  const field = (k) => {
    const cur = m[k] === null || m[k] === undefined ? "" : String(m[k]);
    if (k === "Function") {
      const s = modeSelect(cur, (e) => { edited.Function = e.target.value; changed(); });
      return label(k, s);
    }
    const input = el("input", { type: k === "Pass" ? "password" : "text", value: cur, oninput: (e) => { edited[k] = e.target.value; changed(); } });
    return label(k, input);
  };
  const pane = (keys) => el("div", { class: "fields" }, keys.filter((k) => k in m).map(field));
  const rest = Object.keys(m).filter((k) =>
    !["Id"].includes(k) && ![...BASIC, ...CONNECTION, ...VIDEO, ...MOTION, ...CONTROL].includes(k));

  const zoneDraft = {};
  const motion = pane(MOTION);
  for (const zone of data.zones) {
    motion.append(el("fieldset", { class: "zone-settings" }, el("legend", {}, zone.Name),
      el("div", { class: "fields" }, state.zoneFields.map((f) => {
        const input = f.options
          ? el("select", {}, f.options.map((v) => el("option", { value: v }, v)))
          : el("input", { type: "number", min: 0, step: "any" });
        input.value = zone[f.name] ?? "";
        input.addEventListener("input", () => {
          (zoneDraft[zone.Id] ||= {})[f.name] = input.value;
          changed();
        });
        return el("label", {}, f.label, el("span", { class: "field-key" }, f.name), input);
      }))));
  }
  const testStatus = el("p", { class: "hint", role: "status" }, "Checking the isolated motion worker…");
  const replay = el("div", {});
  const test = el("button", { disabled: true }, "Test unsaved settings");
  motion.prepend(el("div", { class: "motion-test card pad" },
    el("strong", {}, "Preview motion detection"),
    el("p", { class: "hint" }, "Capture a 10-second sample at 10 fps and replay it with your draft thresholds in an isolated ZoneMinder engine. Live recording and alerts stay unchanged. Connection, video and recording-mode edits are not part of this test."),
    test, testStatus, replay));
  get("motion/capability").then((c) => {
    test.disabled = !c.available || !data.zones.length;
    testStatus.textContent = !data.zones.length ? "Create a watched area first." : c.available ? "Ready · ZoneMinder " + c.engine : c.reason;
  }).catch((e) => { testStatus.textContent = e.message; });
  test.addEventListener("click", async () => {
    test.disabled = true;
    killStreams(replay); replay.replaceChildren();
    const tested = JSON.stringify({ monitor: edited, zones: zoneDraft });
    try {
      const monitor = Object.fromEntries(Object.entries(edited).filter(([k]) =>
        ["AlarmFrameCount", "PreEventCount", "PostEventCount", "AnalysisFPS", "AnalysisFPSLimit", "RefBlendPerc", "AlarmRefBlendPerc"].includes(k)));
      let job = await post("motion/test", { id, monitor, zones: zoneDraft });
      while (["capturing", "analysing"].includes(job.state)) {
        testStatus.textContent = job.state === "capturing" ? "Capturing a fresh 10-second sample…" : "Analysing your sample in isolation…";
        await new Promise((resolve) => setTimeout(resolve, 1500));
        if (!body.isConnected || $("#modal").classList.contains("hidden") || !body.contains(preview)) return;
        job = await get("motion/status", { id: job.id });
      }
      if (job.state !== "ready") throw new Error(job.error || "Motion test failed.");
      replay.append(el("video", { controls: true, playsinline: true, src: "/api/motion/file?id=" + job.id, "aria-label": "Motion test replay with detection overlays" }));
      testStatus.textContent = job.alarm_frames + " of " + job.frames + " frames triggered detection. Highlighted areas show where ZoneMinder detected motion. " +
        (tested !== JSON.stringify({ monitor: edited, zones: zoneDraft }) ? "Settings changed during the test; test again to preview the latest draft." : "These settings have not been applied.");
    } catch (e) { testStatus.textContent = e.message; }
    finally { test.disabled = false; }
  });
  let peopleOnly = null;
  if (data.smart) {
    motion.prepend(el("label", { class: "check" },
      el("input", { type: "checkbox", checked: !!data.people_only,
        onchange: (e) => { peopleOnly = e.target.checked; changed(); } }),
      "Alert my phone only when somebody is seen"));
  }

  const tabs = { "Basics": pane(BASIC), "Connection": pane(CONNECTION), "Video": pane(VIDEO),
    "Motion": motion, "Control": pane(CONTROL), "Advanced": pane(rest) };
  let selectedTab = "Basics";
  const holder = el("div", {});
  const search = el("input", { type: "search", placeholder: "Find a camera setting…", "aria-label": "Search camera settings" });
  search.addEventListener("input", () => {
    const query = search.value.trim().toLowerCase();
    for (const tab of Object.values(tabs)) {
      $$("label", tab).forEach((field) => field.classList.toggle("hidden", !!query && !field.textContent.toLowerCase().includes(query)));
    }
    holder.replaceChildren(...(query ? Object.values(tabs) : [tabs[selectedTab]]));
  });
  const bar = el("div", { class: "tabs" }, Object.keys(tabs).map((name, i) =>
    el("button", { class: i === 0 ? "active" : "", onclick: (e) => {
      selectedTab = name;
      search.value = "";
      Object.values(tabs).forEach((tab) => $$("label", tab).forEach((field) => field.classList.remove("hidden")));
      $$(".tabs button", bar).forEach((b) => b.classList.remove("active"));
      e.target.classList.add("active");
      holder.replaceChildren(tabs[name]);
    } }, name)));
  holder.replaceChildren(tabs["Basics"]);

  apply.addEventListener("click", async () => {
    modalBusy = true;
    const controls = Object.values(tabs).flatMap((tab) => $$("input, select, button", tab)).filter((c) => !c.disabled);
    controls.forEach((c) => { c.disabled = true; });
    apply.disabled = true;
    apply.textContent = "Applying…";
    dirtyNote.textContent = "Saving changes";
    try {
      if (Object.keys(edited).length) {
        await post("camera/save", Object.assign({ id }, edited));
        Object.keys(edited).forEach((k) => delete edited[k]);
      }
      for (const zid of Object.keys(zoneDraft)) {
        await post("zone/update", { monitor: id, zones: { [zid]: zoneDraft[zid] } });
        delete zoneDraft[zid];
      }
      if (peopleOnly !== null) await post("camera/people-only", { id, on: peopleOnly });
      dirty = false;
      $("#modal").dataset.dirty = "";
      modalBusy = false;
      closeModal();
      toast("Camera settings applied.");
      PAGES.cameras();
    } catch (e) {
      dirtyNote.textContent = "Some changes may not have applied — review and try again";
      apply.textContent = "Try again";
      apply.disabled = false;
      fail(e);
    } finally { modalBusy = false; controls.forEach((c) => { c.disabled = false; }); }
  });

  const preview = el("div", { class: "settings-preview" },
    el("div", { class: "pane" },
      el("img", { src: data.stream, alt: "Live view from " + m.Name })),
    el("div", { class: "preview-meta" }, el("span", {}, "Live preview"), el("span", {}, m.Width + " × " + m.Height)),
    el("button", { onclick: () => {
      if (modalBusy) return;
      if (dirty && !confirm("Discard unsaved camera changes and edit watched areas?")) return;
      dirty = false;
      $("#modal").dataset.dirty = "";
      zoneEditor(id);
    } }, "Edit watched areas"));
  const body = modal(
    el("div", { class: "settings-head" }, el("p", { class: "eyebrow" }, "Camera settings"), el("h2", {}, m.Name)),
    el("div", { class: "settings-layout" }, preview,
      el("div", { class: "settings-fields" }, search, bar, holder)),
    el("div", { class: "settings-actions" }, dirtyNote,
      el("button", { onclick: closeModal }, "Cancel"), apply));
  body.classList.add("settings-sheet");
}

// --- zone editor ------------------------------------------------------------

async function zoneEditor(id) {
  const data = await get("camera", { id });
  const m = data.monitor;
  const zones = data.zones;
  const W = 480, H = Math.round(480 * (m.Height || 1080) / (m.Width || 1920));
  const scaleX = (m.Width || 1920) / W, scaleY = (m.Height || 1080) / H;

  let points = [];
  let current = zones[0] || null;
  const canvas = el("canvas", { width: W, height: H });
  const img = el("img", { src: data.stream, width: W, height: H, alt: "" });
  const ctx = canvas.getContext("2d");
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();

  function draw() {
    ctx.clearRect(0, 0, W, H);
    if (!points.length) return;
    ctx.beginPath();
    points.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.closePath();
    ctx.fillStyle = accent;
    ctx.strokeStyle = accent;
    ctx.lineWidth = 2;
    ctx.globalAlpha = 0.28;
    ctx.fill();
    ctx.globalAlpha = 1;
    ctx.stroke();
    points.forEach(([x, y]) => {
      ctx.fillStyle = "#fff";
      ctx.fillRect(x - 3, y - 3, 6, 6);
      ctx.strokeRect(x - 3, y - 3, 6, 6);
    });
  }

  function load(z) {
    current = z;
    if (typeof loadAdvanced === "function") loadAdvanced(z);
    name.value = z ? z.Name : "New area";
    type.value = z ? z.Type : "Active";
    points = z && z.Coords
      ? z.Coords.trim().split(/\s+/).map((p) => {
          const [x, y] = p.split(",").map(Number);
          return [Math.round(x / scaleX), Math.round(y / scaleY)];
        })
      : [];
    draw();
  }

  canvas.addEventListener("click", (e) => {
    const r = canvas.getBoundingClientRect();
    points.push([Math.round(e.clientX - r.left), Math.round(e.clientY - r.top)]);
    draw();
  });

  const name = el("input", {});
  const type = el("select", {}, state.zoneTypes.map((t) =>
    el("option", { value: t.value }, t.label)));
  const level = el("select", {}, ["Low", "Normal", "High"].map((s) =>
    el("option", { value: s, selected: s === "Normal" }, s)));
  const picker = el("select", { onchange: (e) => load(zones[e.target.value] || null) },
    zones.map((z, i) => el("option", { value: i }, z.Name)),
    el("option", { value: "new" }, "— new area —"));

  // Every remaining ZoneMinder zone column, so nothing is out of reach.
  if (!state.zoneFields) state.zoneFields = await get("zonefields");
  const advanced = {};
  const advancedFields = el("div", { class: "fields" }, state.zoneFields.map((f) => {
    const input = f.options
      ? el("select", {}, f.options.map((o) => el("option", { value: o }, o)))
      : el("input", { type: "number" });
    advanced[f.name] = input;
    return el("label", {}, f.label + " (" + f.name + ")", input);
  }));

  function loadAdvanced(z) {
    for (const [key, input] of Object.entries(advanced)) {
      const v = z && z[key] !== null && z[key] !== undefined ? z[key] : "";
      input.value = v;
    }
  }

  load(current);
  loadAdvanced(current);

  modal(el("h2", {}, "Watched areas — " + m.Name),
    el("p", { class: "hint" }, "Click the picture to place corners. Movement is only noticed inside the shape."),
    el("div", { class: "zone-wrap" }, img, canvas),
    el("div", { class: "fields" },
      el("label", {}, "Area", picker), el("label", {}, "Name", name),
      el("label", {}, "Kind", type), el("label", {}, "Sensitivity", level)),
    el("details", {}, el("summary", {}, "Show advanced"),
      el("p", { class: "hint" },
        "Blank means leave ZoneMinder's own value alone. The sensitivity above "
        + "rewrites the alarm pixel counts when you use it."),
      advancedFields),
    el("div", { class: "row" },
      el("button", { onclick: () => { points = []; draw(); } }, "Start over"),
      el("button", { onclick: () => { points.pop(); draw(); } }, "Undo corner"),
      el("button", { onclick: () => {
        points = [[4, 4], [W - 4, 4], [W - 4, H - 4], [4, H - 4]]; draw();
      } }, "Whole picture"),
      current && el("button", { class: "danger", onclick: async () => {
        try { await post("zone/delete", { id: current.Id }); closeModal(); toast("Area deleted."); }
        catch (e) { fail(e); }
      } }, "Delete area"),
      el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: async () => {
        if (points.length < 3) return toast("Place at least three corners.", true);
        try {
          await post("zone/save", {
            id: current ? current.Id : null, monitor: id, name: name.value || "Zone",
            type: type.value, level: level.value,
            points: points.map(([x, y]) => [Math.round(x * scaleX), Math.round(y * scaleY)]),
            advanced: Object.fromEntries(Object.entries(advanced)
              .map(([k, input]) => [k, input.value])
              .filter(([, v]) => v !== "")),
          });
          closeModal(); toast("Area saved.");
        } catch (e) { fail(e); }
      } }, "Save area")));
}

// --- ptz --------------------------------------------------------------------

function ptzPad(c) {
  const send = async (command, preset) => {
    try { await post("ptz", { id: c.id, command, preset }); }
    catch (e) { fail(e); }
  };
  const b = (label, cmd) => el("button", { onclick: () => send(cmd) }, label);
  const preset = el("input", { type: "number", min: "1", max: "10", value: "1", style: "width:70px" });
  modal(el("h2", {}, "Move " + c.name),
    el("div", { class: "ptz" },
      el("span"), b("↑", "moveConUp"), el("span"),
      b("←", "moveConLeft"), b("■", "moveStop"), b("→", "moveConRight"),
      el("span"), b("↓", "moveConDown"), el("span")),
    el("div", { class: "row" }, b("Zoom in", "zoomConTele"), b("Zoom out", "zoomConWide")),
    el("div", { class: "row" }, "Preset", preset,
      el("button", { onclick: () => send("presetGoto", preset.value) }, "Go"),
      el("button", { onclick: () => send("presetSet", preset.value) }, "Save here")),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Close")));
}

// --- page: live view --------------------------------------------------------

let liveTimer = null;
let liveRefresh = null;

function stopLive(clear) {
  if (liveTimer) { clearInterval(liveTimer); liveTimer = null; }
  if (liveRefresh) { clearInterval(liveRefresh); liveRefresh = null; }
  killStreams($("#live-grid"));
  if (clear) $("#live-grid").replaceChildren();
}

PAGES.live = async function () {
  state.cameras = await get("cameras");
  drawLive();
};

$("#live-layout").addEventListener("change", drawLive);
$("#live-quality").addEventListener("change", drawLive);
$("#live-cycle").addEventListener("change", drawLive);

let liveOffset = 0;

function drawLive() {
  stopLive(false);
  const n = Number($("#live-layout").value);
  const scale = $("#live-quality").value;
  const grid = $("#live-grid");
  grid.className = "live-grid n" + n;
  // Browsers allow ~6 sockets per origin, so 9/16-up grids can never hold a
  // stream per pane; they show stills refreshed in place instead.
  const mjpeg = n <= 4;
  if (state.cameras.length <= n) liveOffset = 0;

  const render = () => {
    const cams = state.cameras;
    killStreams(grid);
    const panes = [];
    for (let i = 0; i < n; i++) {
      const c = cams.length > n ? cams[(liveOffset + i) % cams.length] : cams[i];
      if (!c) { panes.push(el("div", { class: "pane empty" }, "No camera")); continue; }
      const src = mjpeg ? streamSrc(c.stream, scale)
        : c.snapshot.replace(/scale=\d+/, "scale=" + scale).replace(/rand=\d+/, "rand=" + Date.now());
      panes.push(el("div", { class: "pane" },
        el("img", { src, alt: c.name, role: "button", tabindex: "0", "aria-label": "Watch " + c.name,
          onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); watch(c); } }, onclick: () => watch(c) }),
        el("div", { class: "label" }, c.name)));
    }
    grid.replaceChildren(...panes);
  };
  render();

  if (!mjpeg) {
    liveRefresh = setInterval(() => $$(".pane img", grid).forEach((i) => {
      i.src = i.src.replace(/rand=\d+/, "rand=" + Date.now());
    }), 3000);
  }
  if ($("#live-cycle").checked && state.cameras.length > n) {
    liveTimer = setInterval(() => { liveOffset = (liveOffset + n) % state.cameras.length; render(); }, 8000);
  }
}

// --- page: recordings -------------------------------------------------------

let recPage = 1;
let recHour = null;
const today = () => new Date().toLocaleDateString("sv");   // YYYY-MM-DD, local time

PAGES.recordings = async function () {
  if (!state.cameras.length) state.cameras = await get("cameras");
  const sel = $("#rec-camera");
  if (sel.options.length <= 1) {
    state.cameras.forEach((c) => sel.append(el("option", { value: c.id }, c.name)));
  }
  recPage = 1;
  drawTimeline().catch(() => {});
  await loadRecordings(true);
};

// One cell per hour of the chosen day; click narrows the list to that hour.
async function drawTimeline() {
  const t = await get("timeline", { camera: $("#rec-camera").value,
                                    day: $("#rec-date").value || today() });
  const max = Math.max(...t.hours, 1);
  $("#rec-timeline").replaceChildren(...t.hours.map((n, h) =>
    el("button", {
      class: "cell" + (recHour === h ? " sel" : ""),
      "aria-label": String(h).padStart(2, "0") + ":00, " + n + " recordings",
      "aria-pressed": recHour === h,
      style: "opacity:" + (n ? (0.3 + 0.7 * n / max).toFixed(2) : 0.08),
      title: String(h).padStart(2, "0") + ":00 — " + n + (n === 1 ? " recording" : " recordings"),
      onclick: () => {
        recHour = recHour === h ? null : h;
        recPage = 1;
        loadRecordings(true).catch(fail);
        drawTimeline().catch(() => {});
      },
    })));
}

["#rec-camera", "#rec-date", "#rec-cause", "#rec-kept", "#rec-sort", "#rec-people"].forEach((s) =>
  $(s).addEventListener("change", () => {
    recPage = 1;
    if (s === "#rec-camera" || s === "#rec-date") recHour = null;
    loadRecordings(true).catch(fail);
    drawTimeline().catch(() => {});
  }));
$("#rec-more").addEventListener("click", () => { recPage++; loadRecordings(false).catch(fail); });

async function loadRecordings(reset) {
  const grid = $("#rec-grid");
  if (reset) grid.replaceChildren(el("p", { class: "muted" }, "Loading..."));
  const day = $("#rec-date").value;
  const base = day || today();
  const hh = recHour === null ? null : String(recHour).padStart(2, "0");
  const params = {
    camera: $("#rec-camera").value, cause: $("#rec-cause").value,
    archived: $("#rec-kept").checked ? "1" : "", page: recPage,
    people: $("#rec-people").checked ? "1" : "",
    sort: $("#rec-sort").value,
    from: hh ? base + " " + hh + ":00:00" : (day ? day + " 00:00:00" : ""),
    to: hh ? base + " " + hh + ":59:59" : (day ? day + " 23:59:59" : ""),
  };
  const r = await get("events", params);
  const cards = r.events.map(recCard);
  if (reset) {
    grid.replaceChildren(...(cards.length ? cards : [el("p", { class: "muted" },
      "Nothing recorded yet — recordings appear here when a camera sees movement.")]));
  } else {
    grid.append(...cards);
  }
  $("#rec-more").classList.toggle("hidden", !r.events.length || r.events.length < 60);
}

function recCard(e) {
  const cam = state.cameras.find((c) => String(c.id) === String(e.monitor));
  return el("div", { class: "card" },
    el("img", { class: "thumb", src: e.thumb, alt: "", loading: "lazy",
                onclick: () => playEvent(e) }),
    el("div", { class: "body" },
      el("div", { class: "title" }, (cam ? cam.name : "Camera " + e.monitor),
        e.person && el("span", { class: "badge" }, "Somebody"),
        e.animal && el("span", { class: "badge" }, "Animal")),
      el("div", { class: "muted" }, e.start + " · " + Math.round(Number(e.length || 0)) + "s"
        + (e.cause ? " · " + e.cause : "")
        + (Number(e.score) ? " · activity " + e.score : "")),
      el("div", { class: "actions" },
        el("button", { onclick: () => playEvent(e) }, "Play"),
        el("a", { href: e.video, download: "" }, el("button", {}, "Save")),
        el("button", { onclick: async () => {
          try {
            const r = await post("share", { id: e.id });
            if (!r.ok) return toast(r.error, true);
            try { await navigator.clipboard.writeText(r.url); } catch (_) { prompt("Copy this link:", r.url); }
            toast("Link copied. Anyone with it can watch this recording for 3 days.");
          } catch (err) { fail(err); }
        } }, "Share"),
        el("button", { onclick: () => findModal(e) }, "Find this"),
        el("button", { onclick: async () => {
          try { await post("event/action", { id: e.id, action: e.archived ? "unkeep" : "keep" });
            toast(e.archived ? "No longer kept." : "Kept forever."); loadRecordings(true); }
          catch (err) { fail(err); }
        } }, e.archived ? "Stop keeping" : "Keep forever"),
        el("button", { class: "danger", onclick: async () => {
          if (!confirm("Delete this recording?")) return;
          try { await post("event/action", { id: e.id, action: "delete" }); toast("Deleted."); loadRecordings(true); }
          catch (err) { fail(err); }
        } }, "Delete"))));
}

function playEvent(e) {
  const video = el("video", { src: e.video, controls: true, autoplay: true });
  video.addEventListener("error", () => {
    // ponytail: some builds can't serve mp4; fall back to ZM's jpeg replay stream.
    video.replaceWith(el("img", { src: e.replay, alt: "" }));
  });
  viewer(video, el("div", { class: "bar" },
    el("span", { style: "color:#ddd" }, e.start),
    el("button", { onclick: closeViewer }, "Close")));
}

// --- the whole day in one short video ---------------------------------------

$("#rec-summary").addEventListener("click", async () => {
  const day = $("#rec-date").value || today();
  const camera = $("#rec-camera").value;
  const msg = el("p", {}, "Reading the day's recordings...");
  modal(el("h2", {}, "Your day in a minute"), msg,
    el("p", { class: "hint" }, "Every recording of " + day + ", sped up and joined "
      + "together. The first build takes a while; after that it is instant."),
    el("div", { class: "row" }, el("button", { onclick: closeModal }, "Close")));
  for (;;) {
    let r;
    try { r = await get("summary", { day: day, camera: camera }); }
    catch (e) { msg.textContent = e.message; return; }
    if (r.error) { msg.textContent = r.error; return; }
    if (!r.building) {
      closeModal();
      return viewer(el("video", { src: r.url, controls: true, autoplay: true }),
        el("div", { class: "bar" }, el("span", { style: "color:#ddd" }, day),
          el("button", { onclick: closeViewer }, "Close")));
    }
    msg.textContent = "Building... " + (r.progress || 0) + "%";
    await new Promise((ok) => setTimeout(ok, 2000));
  }
});

// --- find one thing across the recordings -----------------------------------

function findModal(e) {
  // Drag a rectangle over the thing, then search the recordings for it.
  const img = el("img", { src: e.thumb, alt: "", style: "max-width:100%;display:block" });
  const box = el("div", { class: "findbox hidden" });
  const wrap = el("div", { class: "findwrap" }, img, box);
  const days = el("select", {},
    el("option", { value: "1" }, "Today"), el("option", { value: "3" }, "Last 3 days"));
  const out = el("p", { class: "hint" }, "Drag a rectangle around the thing.");
  let a = null, sel = null;
  const at = (ev) => {
    const r = img.getBoundingClientRect();
    return [(ev.clientX - r.left) / r.width, (ev.clientY - r.top) / r.height];
  };
  wrap.addEventListener("pointerdown", (ev) => { a = at(ev); ev.preventDefault(); });
  wrap.addEventListener("pointermove", (ev) => {
    if (!a) return;
    const b = at(ev);
    sel = [Math.min(a[0], b[0]), Math.min(a[1], b[1]),
           Math.abs(b[0] - a[0]), Math.abs(b[1] - a[1])];
    box.classList.remove("hidden");
    box.style.cssText = "left:" + sel[0] * 100 + "%;top:" + sel[1] * 100
      + "%;width:" + sel[2] * 100 + "%;height:" + sel[3] * 100 + "%";
  });
  wrap.addEventListener("pointerup", () => { a = null; });

  modal(el("h2", {}, "Find this"), wrap,
    el("div", { class: "row" },
      el("label", {}, "Search ", days),
      el("button", { class: "primary", onclick: () => start() }, "Search"),
      el("button", { onclick: closeModal }, "Close")),
    out);

  async function start() {
    if (!sel || sel[2] < 0.02 || sel[3] < 0.02) return toast("Drag a rectangle first.", true);
    const r = await post("find", { id: e.id, box: sel, camera: e.monitor, days: days.value });
    if (!r.ok) return toast(r.error, true);
    for (;;) {
      const j = await get("job");
      if (j.error) { out.textContent = j.error; return; }
      if (!j.running && j.result) {
        out.replaceChildren(el("strong", {}, j.result.verdict),
          el("div", { class: "muted" }, "Looked through " + j.result.scanned + " recordings."),
          ...j.result.seen.map((s) => el("div", { class: "item" },
            el("span", {}, (s.start || "") + " · " + s.at + "s in"),
            el("button", { onclick: () => playEvent(s) }, "Play"))));
        return;
      }
      out.textContent = "Searching... " + (j.progress || 0) + "%";
      await new Promise((ok) => setTimeout(ok, 1500));
    }
  }
}

// --- page: rules ------------------------------------------------------------

PAGES.rules = async function () {
  if (!state.cameras.length) state.cameras = await get("cameras");
  const list = $("#rule-list");
  const rules = await get("rules");
  list.replaceChildren(...(rules.length ? rules.map(ruleItem)
    : [el("p", { class: "muted" },
        "No rules yet — a rule says what happens when a camera records something.")]));
  loadPushSettings(rules);
  loadEmailSettings().catch(() => {});
};

function ruleItem(r) {
  const does = [r.email && "sends email", r.push && "alerts your phone",
    r.keep && "keeps the recording",
    r.delete && "deletes old recordings", r.command && "runs a program"].filter(Boolean);
  const named = (r.cameras || []).map((id) => {
    const c = state.cameras.find((x) => String(x.id) === String(id));
    return c ? c.name : "camera " + id;
  });
  const when = (r.what === "motion" ? "When movement is recorded" : "For every recording")
    + (named.length ? " on " + named.join(" and ") : " on any camera")
    + (r.between ? ", between " + r.between[0] + " and " + r.between[1] : "")
    + (r.days === "weekdays" ? ", on weekdays" : r.days === "weekends" ? ", at weekends" : "");
  return el("div", { class: "item" },
    el("div", {}, el("strong", {}, r.name),
      el("div", { class: "muted" }, when + (does.length ? ", it " + does.join(" and ") : ""))),
    el("div", { class: "row" },
      el("button", { onclick: () => ruleModal(r) }, "Edit"),
      el("button", { class: "danger", onclick: async () => {
        if (!confirm("Delete rule \u201c" + r.name + "\u201d?")) return;
        try { await post("rule/delete", { id: r.id }); toast("Rule deleted."); PAGES.rules(); }
        catch (e) { fail(e); }
      } }, "Delete")));
}

$("#btn-rule").addEventListener("click", () => ruleModal(null));

function ruleModal(r) {
  r = r || {};
  const picked = (r.cameras || []).map(String);
  const name = el("input", { value: r.name || "My rule" });
  const what = el("select", {},
    el("option", { value: "motion", selected: r.what === "motion" }, "something moves"),
    el("option", { value: "any", selected: r.what !== "motion" }, "anything is recorded"));
  const cams = el("select", { multiple: true, size: 4 },
    state.cameras.map((c) => el("option", { value: c.id, selected: picked.includes(String(c.id)) },
                               c.name)));
  const from = el("input", { type: "time", value: (r.between || [])[0] || "" });
  const to = el("input", { type: "time", value: (r.between || [])[1] || "" });
  const daysSel = el("select", {},
    el("option", { value: "", selected: !r.days }, "Any day"),
    el("option", { value: "weekdays", selected: r.days === "weekdays" }, "Weekdays"),
    el("option", { value: "weekends", selected: r.days === "weekends" }, "Weekends"));
  const email = el("input", { type: "checkbox", checked: !!r.email });
  const push = el("input", { type: "checkbox", checked: !!r.push });
  const keep = el("input", { type: "checkbox", checked: !!r.keep });
  const days = el("input", { type: "number", min: "0", placeholder: "0", style: "width:90px",
                             value: r.delete_after_days || "" });
  const blips = el("input", { type: "number", min: "0", placeholder: "0", style: "width:90px",
                              value: r.min_frames || "" });
  const cmd = el("input", { placeholder: "/usr/local/bin/my-script", value: r.command || "" });

  const form = modal(el("h2", {}, r.id ? "Edit rule" : "New rule"),
    el("div", { class: "row" }, "When ", what, " on "),
    el("div", { class: "fields" },
      el("label", {}, "these cameras (none = all)", cams),
      el("label", {}, "Name", name),
      el("label", {}, "Between (optional)", el("div", { class: "row" }, from, "and", to)),
      el("label", {}, "On", daysSel)),
    el("div", { class: "list" },
      el("label", { class: "check" }, email, "Email me"),
      el("label", { class: "check" }, push, "Send an alert to my phone"),
      el("label", { class: "check" }, keep, "Keep the recording forever")),
    el("details", { open: state.page === "system" }, el("summary", {}, "Technical settings"),
      el("div", { class: "fields" },
        el("label", {}, "Ignore blips with fewer movement frames than (0 = keep all)", blips),
        el("label", {}, "Delete recordings older than (days, 0 = never)", days),
        el("label", {}, "Run this program", cmd))),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: async () => {
        try {
          await post("rule/save", {
            id: r.id || null,
            name: name.value.trim() || "Rule",
            cameras: [...cams.selectedOptions].map((o) => o.value),
            what: what.value,
            between: from.value && to.value ? [from.value, to.value] : null,
            days: daysSel.value || null,
            min_frames: Number(blips.value) || null,
            email: email.checked, keep: keep.checked,
            push: push.checked ? pushTopic() : null,
            push_server: pushServer(),
            delete_after_days: Number(days.value) || null,
            command: cmd.value.trim() || null,
          });
          $("#modal").dataset.dirty = "";
          closeModal(); toast("Rule saved."); PAGES[state.page]();
        } catch (e) { fail(e); }
      } }, "Save rule")));
  form.addEventListener("input", () => { $("#modal").dataset.dirty = "1"; });
}

// --- phone alerts (ntfy) ----------------------------------------------------
// The topic is the whole secret, so it is long and random. Rules carry it inside
// the command they run; this page only remembers it for the next new rule.

function newTopic() {
  const bytes = crypto.getRandomValues(new Uint8Array(9));
  return "porchlight-" + [...bytes].map((b) => b.toString(36)).join("").slice(0, 12);
}

function pushTopic() {
  if (!localStorage.ntfyTopic) localStorage.ntfyTopic = newTopic();
  return localStorage.ntfyTopic;
}

const pushServer = () => localStorage.ntfyServer || "https://ntfy.sh";

function loadPushSettings(rules) {
  // A rule already pushing wins: that is the topic the phone is subscribed to.
  const used = (rules || []).map((r) => r.push).filter(Boolean)[0];
  if (used) localStorage.ntfyTopic = used;
  $("#push-topic").value = pushTopic();
  $("#push-server").value = pushServer();
}

$("#push-topic").addEventListener("change", (e) => {
  localStorage.ntfyTopic = e.target.value.trim() || newTopic();
  e.target.value = localStorage.ntfyTopic;
});
$("#push-server").addEventListener("change", (e) => {
  localStorage.ntfyServer = e.target.value.trim() || "https://ntfy.sh";
  e.target.value = pushServer();
});
$("#push-new").addEventListener("click", () => {
  localStorage.ntfyTopic = newTopic();
  $("#push-topic").value = localStorage.ntfyTopic;
  $("#push-msg").textContent = "New topic. Subscribe your phone to it, and re-save any rules.";
});
$("#push-test").addEventListener("click", async () => {
  $("#push-msg").textContent = "Sending...";
  try {
    const r = await post("test-push", { topic: $("#push-topic").value, server: $("#push-server").value });
    $("#push-msg").textContent = r.ok ? "Sent. Check your phone."
      : "Failed: " + (r.out || "").slice(-200);
  } catch (e) { $("#push-msg").textContent = "Failed: " + e.message; }
});

const EMAIL_KEYS = { "email-on": "ZM_OPT_EMAIL", "email-host": "ZM_EMAIL_HOST",
  "email-to": "ZM_EMAIL_ADDRESS", "email-from": "ZM_FROM_EMAIL" };

async function loadEmailSettings() {
  const rows = await get("configs", { q: "email" });
  const byName = Object.fromEntries(rows.map((r) => [r.name, r.value]));
  for (const [id, key] of Object.entries(EMAIL_KEYS)) {
    const node = $("#" + id);
    if (node.type === "checkbox") node.checked = byName[key] === "1";
    else node.value = byName[key] || "";
  }
}

$("#email-save").addEventListener("click", async () => {
  try {
    for (const [id, key] of Object.entries(EMAIL_KEYS)) {
      const node = $("#" + id);
      await post("config/set", { name: key, value: node.type === "checkbox" ? (node.checked ? "1" : "0") : node.value });
    }
    $("#email-msg").textContent = "Saved.";
  } catch (e) { fail(e); }
});

$("#email-test").addEventListener("click", async () => {
  $("#email-msg").textContent = "Sending...";
  try {
    const r = await post("test-email", { to: $("#email-to").value });
    $("#email-msg").textContent = r.ok ? "Sent. Check your inbox." : "Failed: " + (r.out || "").slice(-200);
  } catch (e) { $("#email-msg").textContent = "Failed: " + e.message; }
});

// --- page: home / away ------------------------------------------------------

PAGES.modes = async function () {
  state.cameras = await get("cameras");
  const states = await get("states");
  const names = ["Home", "Away"];
  states.forEach((s) => { if (!names.includes(s.Name)) names.push(s.Name); });

  const active = (states.find((s) => s.IsActive === "1" || s.IsActive === 1) || {}).Name;
  $("#state-buttons").replaceChildren(...names.map((n) =>
    el("button", { class: n === active ? "on" : "", onclick: async () => {
      try { await post("state/apply", { name: n }); toast("Switched to " + n + "."); PAGES.modes(); }
      catch (e) { fail(e); }
    } }, n)));

  // What each mode does is stored in the state's own definition, not in the
  // cameras' current settings, so edit that.
  state.stateDefs = Object.fromEntries(states.map((s) => [s.Name, parseDefinition(s.Definition)]));
  const pick = $("#state-pick");
  pick.replaceChildren(...names.map((n) => el("option", { value: n }, n)));
  drawStateRows();
  pick.onchange = drawStateRows;
  if (!$("#sched-rows").children.length) addSchedRow();
};

function parseDefinition(text) {
  const out = {};
  for (const part of (text || "").split(",")) {
    const [id, fn] = part.split(":");
    if (id && fn) out[id] = fn;
  }
  return out;
}

function drawStateRows() {
  const def = (state.stateDefs || {})[$("#state-pick").value] || {};
  $("#state-rows").replaceChildren(...state.cameras.map((c) => {
    const sel = modeSelect(def[String(c.id)] || c.function);
    sel.dataset.id = c.id;
    return el("div", { class: "item" }, el("span", {}, c.name), sel);
  }));
}

$("#state-save").addEventListener("click", async () => {
  const rows = $$("#state-rows select").map((s) => ({ id: s.dataset.id, function: s.value, enabled: s.value !== "None" }));
  try {
    await post("state/save", { name: $("#state-pick").value, rows });
    $("#state-msg").textContent = "Saved.";
  } catch (e) { fail(e); }
});

function addSchedRow() {
  const st = el("select", {}, [...$("#state-pick").options].map((o) => el("option", { value: o.value }, o.value)));
  const time = el("input", { type: "time", value: "08:00" });
  const days = el("select", {},
    el("option", { value: "*" }, "Every day"),
    el("option", { value: "1-5" }, "Weekdays"),
    el("option", { value: "0,6" }, "Weekends"));
  const row = el("div", { class: "item" },
    el("div", { class: "row" }, "Switch to", st, "at", time, days),
    el("button", { class: "danger", onclick: () => row.remove() }, "Remove"));
  $("#sched-rows").append(row);
}

$("#sched-add").addEventListener("click", addSchedRow);
$("#sched-save").addEventListener("click", async () => {
  const entries = $$("#sched-rows .item").map((row) => {
    const [st, time, days] = [row.querySelector("select"), row.querySelector("input"),
      row.querySelectorAll("select")[1]];
    return { state: st.value, time: time.value, days: days.value };
  });
  try {
    const r = await post("schedule/save", { entries });
    $("#sched-msg").textContent = r.ok ? "Schedule saved." : "Failed: " + (r.out || "").slice(-200);
  } catch (e) { fail(e); }
});

// --- page: people -----------------------------------------------------------

PAGES.people = async function () {
  const users = await get("users");
  $("#user-list").replaceChildren(...users.map((u) =>
    el("div", { class: "item" },
      el("div", {}, el("strong", {}, u.Username),
        el("div", { class: "muted" }, u.System === "Edit" ? "Can change settings" : "Can watch only")),
      el("div", { class: "row" },
        el("button", { onclick: () => userForm(u) }, "Edit"),
        el("button", { class: "danger", onclick: async () => {
          if (!confirm("Remove " + u.Username + "?")) return;
          try { await post("user/delete", { id: u.Id }); toast("Removed."); PAGES.people(); }
          catch (e) { fail(e); }
        } }, "Remove")))));
  const cfg = await get("configs", { q: "OPT_USE_AUTH" });
  const row = cfg.find((c) => c.name === "ZM_OPT_USE_AUTH");
  $("#auth-on").checked = row && row.value === "1";
};

$("#auth-on").addEventListener("change", async (e) => {
  try {
    await post("config/set", { name: "ZM_OPT_USE_AUTH", value: e.target.checked ? "1" : "0" });
    toast(e.target.checked ? "Password protection on. Sign in when asked." : "Password protection off.");
  } catch (err) { fail(err); }
});

$("#btn-user").addEventListener("click", () => userForm(null));

function userForm(u) {
  const name = el("input", { value: u ? u.Username : "" });
  const pw = el("input", { type: "password", placeholder: u ? "(unchanged)" : "" });
  const role = el("select", {},
    el("option", { value: "viewer", selected: !u || u.System !== "Edit" }, "Can watch only"),
    el("option", { value: "manager", selected: u && u.System === "Edit" }, "Can change settings"));
  modal(el("h2", {}, u ? "Edit " + u.Username : "Add a person"),
    el("div", { class: "fields" },
      el("label", {}, "Name", name), el("label", {}, "Password", pw), el("label", {}, "Can do", role)),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { onclick: closeModal }, "Cancel"),
      el("button", { class: "primary", onclick: async () => {
        try {
          await post("user/save", { id: u ? u.Id : null, username: name.value.trim(),
            password: pw.value, role: role.value });
          closeModal(); toast("Saved."); PAGES.people();
        } catch (e) { fail(e); }
      } }, "Save")));
}

// --- page: system -----------------------------------------------------------
let configDraft = {};
const settingsNames = { advanced: "Advanced", storage: "Storage & backup", diagnostics: "Diagnostics", access: "Remote access", integrations: "Integrations" };
function selectSettings(name) {
  $$(".settings-section").forEach((s) => s.classList.toggle("hidden", s.dataset.settings !== name));
  $$("#settings-nav button").forEach((b) => { b.classList.toggle("active", b.dataset.section === name); b.setAttribute("aria-pressed", b.dataset.section === name); });
}
$("#settings-nav").replaceChildren(...Object.entries(settingsNames).map(([key, label]) =>
  el("button", { "data-section": key, onclick: () => selectSettings(key) }, label)));
selectSettings("advanced");
$("#retention-add").addEventListener("click", () => ruleModal({ name: "Recording retention", delete_after_days: 30 }));

function syncConfigDraft() {
  const n = Object.keys(configDraft).length;
  $("#cfg-dirty").textContent = n ? n + " unsaved setting" + (n === 1 ? "" : "s") : "No unsaved changes";
  $("#cfg-apply").disabled = $("#cfg-discard").disabled = !n;
}
$("#cfg-discard").addEventListener("click", () => { configDraft = {}; renderConfigs(allConfigs); syncConfigDraft(); });
$("#cfg-apply").addEventListener("click", async () => {
  const controls = $$("[data-config], #cfg-discard, #cfg-search");
  controls.forEach((c) => { c.disabled = true; });
  $("#cfg-apply").disabled = true;
  try {
    for (const [name, value] of Object.entries(configDraft)) {
      await post("config/set", { name, value });
      const row = allConfigs.find((c) => c.name === name);
      if (row) row.value = value;
      delete configDraft[name];
    }
    toast("Settings applied.");
  } catch (e) { fail(e); }
  controls.forEach((c) => { c.disabled = false; });
  renderConfigs(allConfigs); syncConfigDraft();
});

const SIMPLE_SETTINGS = [
  ["ZM_WEB_REFRESH_MAIN", "How often the camera list refreshes (seconds)"],
  ["ZM_EVENT_IMAGE_DIGITS", "Digits used in recording file names"],
  ["ZM_TIMESTAMP_ON_CAPTURE", "Stamp the time onto the picture"],
  ["ZM_WEB_H_SCALE_THUMBS", "Shrink thumbnails to save space"],
  ["ZM_MAX_RESTART_DELAY", "Give up restarting a camera after (seconds)"],
];

PAGES.system = async function () {
  const s = await refreshHealth();
  const pct = s.storage && s.storage.total ? Math.round(100 * s.storage.used / s.storage.total) : 0;
  $("#sys-summary").replaceChildren(
    el("div", { class: "card pad" }, el("strong", {}, "Camera system"),
      el("div", { class: "muted" }, s.ok ? "Running · ZoneMinder " + s.version : "Not responding"),
      el("div", { class: "muted" }, s.daemon === null ? "" : (s.daemon ? "Recorder is alive" : "Recorder is stopped"))),
    el("div", { class: "card pad" }, el("strong", {}, "Storage"),
      el("div", { class: "bar-track" }, el("div", { class: "bar-fill", style: "width:" + pct + "%" })),
      el("div", { class: "muted" }, s.storage ?
        gb(s.storage.free) + " GB free of " + gb(s.storage.total) + " GB" : ""),
      el("div", { class: "muted" }, "Review deletion rules in Storage & backup and check free space regularly.")));

  const access = await get("access");
  $("#lan-on").checked = access.lan;
  $("#lan-url").textContent = access.url || "(no network address yet)";

  const mq = await get("mqtt");
  $("#mqtt-broker").value = mq.broker || "";
  $("#mqtt-topic").value = mq.topic || "porchlight";
  $("#mqtt-user").value = mq.user || "";

  const cfg = await get("configs");
  const byName = Object.fromEntries(cfg.map((c) => [c.name, c]));
  $("#sys-simple").replaceChildren(...SIMPLE_SETTINGS.filter(([k]) => byName[k]).map(([k, label]) =>
    configRow(byName[k], label)));
  renderConfigs(cfg);
  const rules = await get("rules");
  const retention = rules.filter((r) => r.delete_after_days);
  $("#retention-rules").replaceChildren(...retention.map((r) => el("div", { class: "item" }, el("div", {}, el("strong", {}, r.name), el("p", { class: "hint" }, "Delete unkept recordings after " + r.delete_after_days + " days")), el("button", { onclick: () => ruleModal(r) }, "Edit rule"))));
  if (!retention.length) $("#retention-rules").textContent = "No time-based retention rules. Check Rules & alerts for any disk-space cleanup filters.";
};

function configRow(c, label) {
  // Yes/no options read as 1 and 0 in the database; show them as a tick box.
  const bool = c.type === "boolean";
  const input = bool
    ? el("input", { type: "checkbox", checked: String(c.value) === "1" })
    : el("input", { value: c.value === null ? "" : c.value });
  input.setAttribute("aria-label", c.name);
  input.dataset.config = c.name;
  const value = Object.hasOwn(configDraft, c.name) ? configDraft[c.name] : c.value;
  if (bool) input.checked = String(value) === "1";
  else input.value = value ?? "";
  input.addEventListener("input", () => {
    const next = bool ? (input.checked ? "1" : "0") : input.value;
    if (next === String(c.value ?? "")) delete configDraft[c.name]; else configDraft[c.name] = next;
    $$("[data-config]").filter((other) => other !== input && other.dataset.config === c.name).forEach((other) => {
      if (bool) other.checked = input.checked; else other.value = next;
    });
    syncConfigDraft();
  });
  return el("div", { class: "item config-row" },
    el("div", {}, el("strong", {}, label || c.name),
      el("div", { class: "muted" }, label ? c.name : c.prompt || "")),
    el("div", { class: "row" }, input));
}

let allConfigs = [];
function renderConfigs(rows) {
  allConfigs = rows;
  const byName = Object.fromEntries(rows.map((c) => [c.name, c]));
  $("#sys-simple").replaceChildren(...SIMPLE_SETTINGS.filter(([k]) => byName[k]).map(([k, label]) => configRow(byName[k], label)));
  const needle = $("#cfg-search").value.toLowerCase();
  const shown = rows.filter((r) => !needle || (r.name + (r.prompt || "")).toLowerCase().includes(needle));
  $("#cfg-list").replaceChildren(...shown.map((c) => configRow(c)));
}
$("#cfg-search").addEventListener("input", () => renderConfigs(allConfigs));

$("#lan-save").addEventListener("click", async () => {
  $("#lan-msg").textContent = "Saving...";
  try {
    const r = await post("access/save", { lan: $("#lan-on").checked, password: $("#lan-pw").value });
    if (!r.ok) return ($("#lan-msg").textContent = r.error);
    $("#lan-pw").value = "";
    $("#lan-url").textContent = r.url || "(no network address yet)";
    $("#lan-msg").textContent = r.restarting
      ? "Saved. Reload this page in a moment." : "Saved.";
  } catch (e) { $("#lan-msg").textContent = "Failed: " + e.message; }
});

$("#restore-btn").addEventListener("click", () => $("#restore-file").click());
$("#restore-file").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  if (!f) return;
  $("#restore-msg").textContent = "Restoring...";
  try {
    const r = await (await fetch("/api/restore", { method: "POST", body: f })).json();
    $("#restore-msg").textContent = r.ok ? "Restored " + r.added + " items." : (r.error || "Failed.");
    PAGES.system().catch(() => {});
  } catch (err) { $("#restore-msg").textContent = "Failed: " + err.message; }
  e.target.value = "";
});

$("#mqtt-save").addEventListener("click", async () => {
  const msg = $("#mqtt-msg");
  msg.textContent = "Saving...";
  try {
    const r = await post("mqtt/save", {
      broker: $("#mqtt-broker").value, topic: $("#mqtt-topic").value,
      user: $("#mqtt-user").value, password: $("#mqtt-pw").value,
    });
    $("#mqtt-pw").value = "";
    msg.textContent = r.off ? "Turned off." : (r.ok ? "Connected." : r.error);
  } catch (e) { msg.textContent = e.message; }
});

$("#btn-restart").addEventListener("click", restart);
$("#btn-logs").addEventListener("click", async () => {
  const out = $("#log-out");
  out.classList.remove("hidden");
  out.textContent = "Loading...";
  try { const r = await get("logs", { lines: 200 }); out.textContent = r.out || "(empty)"; }
  catch (e) { out.textContent = e.message; }
});

// --- sign-in when ZoneMinder wants a password -------------------------------

function loginForm() {
  const user = el("input", { value: "admin" });
  const pw = el("input", { type: "password" });
  const go = async () => {
    try {
      const r = await post("login", { user: user.value, password: pw.value });
      if (!r.ok) return toast("Wrong name or password.", true);
      closeModal(); start();
    } catch (e) { fail(e); }
  };
  pw.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  modal(el("h2", {}, "Sign in"),
    el("p", { class: "hint" }, "Your camera system asks for a password."),
    el("div", { class: "fields" }, el("label", {}, "Name", user), el("label", {}, "Password", pw)),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { class: "primary", onclick: go }, "Sign in")));
  pw.focus();
}

// --- boot -------------------------------------------------------------------

// Phones get the app shell and nothing else until they type the password set on
// the System page. On this computer itself the check never fires.
function passwordForm() {
  const pw = el("input", { type: "password" });
  const go = async () => {
    try {
      const r = await post("signin", { password: pw.value });
      if (!r.ok) return toast(r.error || "Wrong password.", true);
      closeModal(); start();
    } catch (e) { fail(e); }
  };
  pw.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  modal(el("h2", {}, "Your cameras"),
    el("p", { class: "hint" }, "Enter the password set on the computer running Porchlight."),
    el("div", { class: "fields" }, el("label", {}, "Password", pw)),
    el("div", { class: "row" }, el("span", { style: "flex:1" }),
      el("button", { class: "primary", onclick: go }, "Sign in")));
  pw.focus();
}

async function start() {
  const session = await get("session").catch(() => ({ signed_in: false }));
  if (!session.signed_in) return passwordForm();
  const s = await refreshHealth();
  if (!s.ok && /401|denied|unauthor/i.test(s.error || "")) return loginForm();
  [state.modes, state.zoneTypes] = await Promise.all([get("modes"), get("zonetypes")]);
  show(location.hash.slice(1) || "cameras");
  setInterval(refreshHealth, 30000);
}

start().catch(fail);
