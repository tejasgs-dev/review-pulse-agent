// ReviewPulse static site. Every number and sentence on the Dashboard and Past weeks pages
// comes from pulses/index.json and pulses/{week}.json (written by `pulse export-site`).
// Text from the data is always inserted as text, never as HTML.
"use strict";

const LINKS = {
  workflow: "https://github.com/tejasgs-dev/review-pulse-agent/actions/workflows/pulse-check.yml",
  badge: "https://github.com/tejasgs-dev/review-pulse-agent/actions/workflows/pulse-check.yml/badge.svg",
  doc: "https://docs.google.com/document/d/1b7MBW40elCdh3cA0V2FCp0TAL9IBuXVyLO4wDPViC_4/edit?usp=sharing",
  agentRepo: "https://github.com/tejasgs-dev/review-pulse-agent",
  mcpRepo: "https://github.com/tejasgs-dev/GmailGdocMCPServer",
};
const HEALTH_URL = "https://gworkspace-mcp.onrender.com/healthz";
const HEALTH_TIMEOUT_MS = 90_000;   // a Render free instance can take ~1 min to wake
const WAKING_AFTER_MS = 5_000;      // show "Waking up…" once a check takes this long
const ATTEMPT_TIMEOUT_MS = 20_000;
const RETRY_DELAY_MS = 3_000;
const RECHECK_AFTER_MS = 60_000;

const STORES = { play_store: "Play Store", app_store: "App Store" };
const LANGUAGES = { en: "English only" };
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// ---------------------------------------------------------------- icons (inline SVG paths)
const ICONS = {
  pulse: "M3 12h4l3-8 4 16 3-8h4",
  external: "M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5",
  close: "M6 6l12 12M18 6L6 18",
  info: "M12 8h.01M11 12h1v5h1M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z",
  calendar: "M8 3v4M16 3v4M4 9h16M5 5h14a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V6a1 1 0 0 1 1-1z",
  chevron: "M6 9l6 6 6-6",
  reviews: "M4 5h16v11H8l-4 4V5zM8 9h8M8 12h5",
  store: "M4 9l1.5-5h13L20 9M4 9v10h16V9M4 9h16M9 13h6",
  thumb: "M7 10v10H4V10h3zM7 10l4-7a2 2 0 0 1 3 2l-1 4h6a2 2 0 0 1 2 2.3l-1.2 6A2 2 0 0 1 17.8 19H7",
  eye: "M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12zM12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6z",
  arrow: "M5 12h14M13 6l6 6-6 6",
  grid: "M4 4h7v7H4zM13 4h7v7h-7zM4 13h7v7H4zM13 13h7v7h-7z",
  list: "M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01",
  download: "M12 3v12M7 10l5 5 5-5M4 19h16",
  shield: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3zM9 12l2 2 4-4",
  cluster: "M12 12m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M5 6m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M19 6m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M5 18m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M19 18m-2 0a2 2 0 1 0 4 0 2 2 0 1 0-4 0M6.5 7.5l4 3M17.5 7.5l-4 3M6.5 16.5l4-3M17.5 16.5l-4-3",
  note: "M6 3h9l4 4v14H6zM14 3v5h5M9 12h7M9 16h5",
  server: "M4 4h16v6H4zM4 14h16v6H4zM8 7h.01M8 17h.01",
  send: "M4 4l16 8-16 8 3-8-3-8zM7 12h13",
  repeat: "M17 2l4 4-4 4M3 11V9a3 3 0 0 1 3-3h15M7 22l-4-4 4-4M21 13v2a3 3 0 0 1-3 3H3",
  quote: "M7 7h4v4c0 3-1.5 5-4 6M15 7h4v4c0 3-1.5 5-4 6",
  code: "M8 8l-4 4 4 4M16 8l4 4-4 4",
  cloud: "M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9a4.5 4.5 0 0 1-.5 9H7z",
  play: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18zM10 8.5l5 3.5-5 3.5v-7z",
  plug: "M9 3v5M15 3v5M6 8h12v3a6 6 0 0 1-12 0V8zM12 17v4",
};

function icon(name, cls = "") {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", `icon ${cls}`.trim());
  svg.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(ns, "path");
  path.setAttribute("d", ICONS[name]);
  svg.appendChild(path);
  return svg;
}

// h("div", {class: "x"}, "text", child): builds DOM; strings become text nodes.
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c == null || c === false) continue;
    el.appendChild(typeof c === "string" || typeof c === "number" ? document.createTextNode(String(c)) : c);
  }
  return el;
}

// ---------------------------------------------------------------- formatting
function pct(share) {
  // Same rule as the pulse: "<1%" for a small non-zero share instead of a misleading 0%.
  if (share > 0 && share < 0.005) return "<1%";
  return `${Math.round(share * 100)}%`;
}
function count(n) { return Number(n).toLocaleString("en-IN"); }
function parseDate(iso) {
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}
function formatWindow(win) {
  const a = parseDate(win.start), b = parseDate(win.end);
  const left = a.y === b.y ? `${a.d} ${MONTHS[a.m - 1]}` : `${a.d} ${MONTHS[a.m - 1]} ${a.y}`;
  return `${left} – ${b.d} ${MONTHS[b.m - 1]} ${b.y}`;
}
function filterLabel(f) {
  return `${f.min_words}+ words · ${LANGUAGES[f.language] || `${f.language} only`}`;
}
function stars(rating) {
  const r = Math.max(0, Math.min(5, Number(rating) || 0));
  return h("span", { class: "stars", "aria-label": `Rated ${r} out of 5` },
    h("span", { "aria-hidden": "true" }, "★".repeat(r)),
    h("span", { class: "off", "aria-hidden": "true" }, "★".repeat(5 - r)));
}

// ---------------------------------------------------------------- data
async function getJSON(path) {
  const res = await fetch(path, { cache: "no-cache" });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}
let indexPromise = null;
function loadIndex() {
  indexPromise ??= getJSON("pulses/index.json");
  return indexPromise;
}
const WEEK_RE = /^\d{4}-W\d{2}$/;

// ---------------------------------------------------------------- shell
function renderShell(page) {
  const navLink = (href, label, key) =>
    h("a", { href, "aria-current": page === key ? "page" : false }, label);

  const statusDot = h("span", { class: "dot", id: "status-dot" });
  const statusBtn = h("button", {
    class: "btn btn-ghost", type: "button", "aria-expanded": "false",
    "aria-controls": "status-popover", id: "status-btn",
  }, statusDot, h("span", {}, "System status"));

  const popover = h("div", { class: "popover", id: "status-popover", role: "dialog",
    "aria-label": "System status", hidden: true },
    h("div", { class: "popover-head" },
      h("h2", {}, "System status"),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Close",
        onclick: () => togglePopover(false) }, icon("close"))),
    statusRow("mcp", "MCP server", "Not checked"),
    statusRow("run", "Last weekly run", ""),
    statusRow("week", "Latest published week", "…"),
    h("p", { class: "popover-note" }, icon("info"),
      h("span", {}, "The MCP server sleeps when idle; the first check can take up to a minute.")));

  const header = document.getElementById("topbar");
  header.className = "topbar";
  header.replaceChildren(h("div", { class: "topbar-inner" },
    h("a", { class: "brand", href: "./" }, icon("pulse"), "ReviewPulse"),
    h("nav", { class: "nav", "aria-label": "Main" },
      navLink("./", "Dashboard", "dashboard"),
      navLink("weeks.html", "Past weeks", "weeks"),
      navLink("how.html", "How it works", "how")),
    h("div", { class: "actions" },
      h("a", { class: "btn", href: LINKS.workflow, target: "_blank", rel: "noopener noreferrer",
        title: "Opens the GitHub Actions workflow in a new tab" },
        h("span", {}, "Pulse check"), icon("external")),
      statusBtn, popover)));

  statusBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    togglePopover(popover.hidden);
  });
  document.addEventListener("click", (e) => {
    if (!popover.hidden && !popover.contains(e.target)) togglePopover(false);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !popover.hidden) { togglePopover(false); statusBtn.focus(); }
  });

  // Workflow badge: GitHub serves the image; until the workflow exists it fails to load.
  const runValue = document.querySelector('[data-status="run"] .value');
  const badge = h("img", { src: LINKS.badge, alt: "Pulse check workflow status", loading: "lazy" });
  badge.addEventListener("error", () => {
    runValue.replaceChildren("Not set up yet");
    setDot("run", "");
  });
  runValue.replaceChildren(h("a", { href: LINKS.workflow, target: "_blank",
    rel: "noopener noreferrer" }, badge));

  const footer = document.getElementById("footer");
  footer.className = "footer";
  const ext = (href, label) => h("a", { href, target: "_blank", rel: "noopener noreferrer" },
    label, icon("external"));
  footer.replaceChildren(
    h("div", { class: "footer-links" },
      ext(LINKS.doc, "Full pulse history (Google Doc)"),
      ext(LINKS.agentRepo, "Agent repo"),
      ext(LINKS.mcpRepo, "MCP server repo")),
    h("p", {}, "ReviewPulse · independent project · built from public app-store reviews"));
}

function statusRow(key, label, value) {
  return h("div", { class: "status-row", "data-status": key },
    h("span", { class: "dot" }), h("span", { class: "label" }, label),
    h("span", { class: "value", "aria-live": key === "mcp" ? "polite" : false }, value));
}
function setDot(key, state, pulsing = false) {
  const dot = document.querySelector(`[data-status="${key}"] .dot`);
  dot.className = `dot ${state}${pulsing ? " pulse" : ""}`.trim();
}
function setValue(key, text) {
  document.querySelector(`[data-status="${key}"] .value`).textContent = text;
}

let lastCheck = { at: 0, running: false };
function togglePopover(open) {
  const popover = document.getElementById("status-popover");
  const btn = document.getElementById("status-btn");
  popover.hidden = !open;
  btn.setAttribute("aria-expanded", String(open));
  if (!open) return;
  loadIndex().then(
    (idx) => { setValue("week", idx.latest || "None yet"); setDot("week", ""); },
    () => { setValue("week", "Unavailable"); setDot("week", "bad"); });
  if (!lastCheck.running && Date.now() - lastCheck.at > RECHECK_AFTER_MS) checkHealth();
}

// Healthy only when /healthz answers {"status": "ok"}. The server needs a CORS header on
// /healthz for the browser to read the answer; without it every attempt fails as "offline".
async function checkHealth() {
  lastCheck.running = true;
  const navDot = document.getElementById("status-dot");
  const show = (state, text, pulsing = false) => {
    setDot("mcp", state, pulsing);
    navDot.className = `dot ${state}${pulsing ? " pulse" : ""}`.trim();
    setValue("mcp", text);
  };
  show("", "Checking…", true);
  const started = Date.now();
  const waking = setTimeout(() => show("warn", "Waking up…", true), WAKING_AFTER_MS);
  let online = false;
  while (Date.now() - started < HEALTH_TIMEOUT_MS) {
    const remaining = HEALTH_TIMEOUT_MS - (Date.now() - started);
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), Math.min(ATTEMPT_TIMEOUT_MS, remaining));
    try {
      const res = await fetch(HEALTH_URL, { cache: "no-store", signal: ctrl.signal });
      if (res.ok && (await res.json()).status === "ok") { online = true; break; }
    } catch { /* network error, CORS block or timeout: try again until the deadline */ }
    finally { clearTimeout(timer); }
    const left = HEALTH_TIMEOUT_MS - (Date.now() - started);
    if (left <= RETRY_DELAY_MS) break;
    await new Promise((r) => setTimeout(r, RETRY_DELAY_MS));
  }
  clearTimeout(waking);
  if (online) show("ok", "Online"); else show("bad", "Offline");
  lastCheck = { at: Date.now(), running: false };
}

// ---------------------------------------------------------------- dashboard
function sectionHead(title, sub, right) {
  return h("div", { class: "section-head" },
    h("div", {}, h("h2", {}, title), sub ? h("p", {}, sub) : null), right || null);
}

async function renderDashboard(root) {
  let idx, data;
  try {
    idx = await loadIndex();
    const asked = new URLSearchParams(location.search).get("week");
    const weeks = idx.weeks.map((w) => w.week);
    const week = asked && WEEK_RE.test(asked) && weeks.includes(asked) ? asked : idx.latest;
    if (!week) return root.replaceChildren(notice("No pulse has been published yet."));
    data = await getJSON(`pulses/${week}.json`);
    if (asked && asked !== week) history.replaceState(null, "", `?week=${week}`);
  } catch (err) {
    console.error(err);
    return root.replaceChildren(notice("The pulse data could not be loaded. Try again later."));
  }
  document.title = `${data.week_label} · ReviewPulse`;

  const select = h("select", { id: "week-select", "aria-label": "Week",
    onchange: (e) => { location.search = `?week=${encodeURIComponent(e.target.value)}`; } },
    idx.weeks.map((w) => h("option", { value: w.week, selected: w.week === data.week },
      `${w.week_label} · ${w.week}`)));

  const playN = data.sources.play_store || 0, appN = data.sources.app_store || 0;
  const total = playN + appN || 1;
  const maxShare = Math.max(...data.themes.map((t) => t.share), 0.0001);

  root.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "ReviewPulse"),
        h("p", {}, "Weekly insights from Groww app reviews")),
      h("div", { class: "select-wrap" }, icon("calendar", "lead"), select, icon("chevron", "trail"))),

    h("div", { class: "grid grid-4" },
      statCard("Total reviews", "reviews", h("div", { class: "stat-value" }, count(data.review_count)),
        h("p", { class: "stat-sub" }, filterLabel(data.filters))),
      statCard("By store", "store",
        h("div", { class: "split", role: "img",
          "aria-label": `Play Store ${count(playN)}, App Store ${count(appN)}` },
          h("span", { style: `width:${(playN / total) * 100}%` }),
          h("span", { style: `width:${(appN / total) * 100}%` })),
        h("div", { class: "legend" },
          h("span", {}, h("i", { class: "swatch" }), "Play Store ", h("b", { class: "num" }, count(playN))),
          h("span", {}, h("i", { class: "swatch light" }), "App Store ", h("b", { class: "num" }, count(appN))))),
      statCard("General praise", "thumb", h("div", { class: "stat-value" }, pct(data.general_praise_share)),
        h("p", { class: "stat-sub" }, `of reviews were general praise · ${pct(data.other_share)} other`)),
      statCard("Date window", "calendar",
        h("div", { class: "stat-value sm" }, formatWindow(data.window)),
        h("p", { class: "stat-sub" }, data.week_label))),

    h("section", { class: "section", "aria-labelledby": "top-themes" },
      sectionHead("Top 3 themes"),
      h("div", { class: "grid grid-3" }, data.top_themes.map((t, i) =>
        h("article", { class: "card theme-card" },
          h("div", { class: "theme-title" }, h("span", { class: "rank" }, i + 1), h("h3", {}, t.label)),
          h("div", { class: "theme-stats" },
            h("span", { class: "stat-value" }, pct(t.share)),
            h("span", { class: "of" }, "of reviews"),
            h("span", { class: "chip", title: "Share of this theme's reviews rated 1 or 2 stars" },
              `${pct(t.negative_share)} 1–2★`)),
          h("p", {}, t.summary))))),

    h("section", { class: "section" },
      h("div", { class: "card" },
        sectionHead("All themes by share", null,
          h("div", { class: "chart-legend" },
            h("span", {}, h("i", { class: "swatch" }), "Top 3"),
            h("span", {}, h("i", { class: "swatch light" }), "Other themes"))),
        h("ul", { class: "bars", "aria-label": "Share of reviews by theme" }, data.themes.map((t) =>
          h("li", {},
            h("span", { class: "bar-label" }, t.label),
            h("span", { class: "bar-track" },
              h("span", { class: `bar${t.top ? "" : " light"}`,
                style: `display:block;width:${(t.share / maxShare) * 100}%` })),
            h("span", { class: "bar-value" }, pct(t.share))))))),

    h("section", { class: "section" },
      sectionHead("What users said"),
      h("div", { class: "grid grid-3" }, data.quotes.map((q) =>
        h("figure", { class: "card quote-card", style: "margin:0" },
          h("span", { class: "quote-mark", "aria-hidden": "true" }, "“"),
          h("blockquote", {}, q.text),
          h("figcaption", { class: "quote-meta" },
            h("span", { class: "chip" }, q.theme_label),
            stars(q.rating),
            h("span", { class: "chip chip-plain" }, STORES[q.source] || q.source)))))),

    h("section", { class: "section" },
      sectionHead("Action ideas"),
      h("ol", { class: "card action-list", style: "padding:0" }, data.actions.map((a, i) =>
        h("li", {},
          h("span", { class: "action-num", "aria-hidden": "true" }, i + 1),
          h("div", { class: "action-body" },
            h("span", { class: "chip" }, a.theme_label),
            h("p", {}, a.action),
            h("p", { class: "watch" }, icon("eye"),
              h("span", {}, h("b", {}, "Watch for: "), a.signal))))))));
}

function statCard(label, iconName, ...body) {
  return h("div", { class: "card stat" },
    h("div", { class: "stat-top" }, h("span", { class: "meta-label" }, label), icon(iconName)),
    ...body);
}
function notice(text) { return h("div", { class: "card notice" }, text); }

// ---------------------------------------------------------------- past weeks
const VIEW_KEY = "reviewpulse.weeksView";
async function renderWeeks(root) {
  let idx;
  try { idx = await loadIndex(); } catch (err) {
    console.error(err);
    return root.replaceChildren(notice("The list of weeks could not be loaded. Try again later."));
  }
  let view = "grid";
  try { view = localStorage.getItem(VIEW_KEY) === "list" ? "list" : "grid"; } catch { /* private mode */ }

  const body = h("div");
  const gridBtn = h("button", { type: "button" }, icon("grid"), "Grid");
  const listBtn = h("button", { type: "button" }, icon("list"), "List");
  const draw = () => {
    gridBtn.setAttribute("aria-pressed", String(view === "grid"));
    listBtn.setAttribute("aria-pressed", String(view === "list"));
    body.replaceChildren(view === "grid" ? weeksGrid(idx) : weeksList(idx));
  };
  const pick = (v) => () => {
    view = v;
    try { localStorage.setItem(VIEW_KEY, v); } catch { /* ignore */ }
    draw();
  };
  gridBtn.addEventListener("click", pick("grid"));
  listBtn.addEventListener("click", pick("list"));

  root.replaceChildren(
    h("div", { class: "page-head" },
      h("div", {}, h("h1", {}, "Past weeks"),
        h("p", {}, "Every weekly pulse, newest first. Open one to see its full dashboard.")),
      h("div", { class: "toggle", role: "group", "aria-label": "Layout" }, gridBtn, listBtn)),
    body);
  draw();
}

const weekHref = (w) => `./?week=${encodeURIComponent(w.week)}`;

function weeksGrid(idx) {
  return h("div", { class: "grid grid-3" },
    idx.weeks.map((w) => h("a", { class: "card week-card", href: weekHref(w) },
      h("div", { class: "week-card-top" },
        h("span", { class: "meta-label" }, w.week),
        w.week === idx.latest ? h("span", { class: "chip" }, "Latest") : null),
      h("div", {}, h("h3", {}, w.week_label), h("p", { class: "window" }, formatWindow(w.window))),
      h("div", { class: "mini-stats" },
        h("div", {}, h("span", {}, "Reviews"), h("b", { class: "num" }, count(w.review_count))),
        h("div", {}, h("span", {}, "General praise"), h("b", { class: "num" }, pct(w.general_praise_share)))),
      h("div", {}, h("p", { class: "meta-label", style: "margin-bottom:6px" }, "Top theme"),
        h("span", { class: "chip" }, `${w.top_theme.label} · ${pct(w.top_theme.share)}`)),
      h("span", { class: "open-link" }, "Open pulse", icon("arrow")))),
    h("div", { class: "card placeholder" },
      h("span", { class: "circle" }, icon("calendar")),
      h("b", {}, "The next pulse appears here after the next weekly run")));
}

function weeksList(idx) {
  return h("div", { class: "card table-card" },
    h("table", { class: "week-table" },
      h("thead", {}, h("tr", {},
        h("th", { class: "meta-label" }, "Week"), h("th", { class: "meta-label" }, "Date window"),
        h("th", { class: "meta-label right" }, "Reviews"), h("th", { class: "meta-label" }, "Top theme"),
        h("th", {}, h("span", { class: "sr-only" }, "Open")))),
      h("tbody", {}, idx.weeks.map((w) => h("tr", {},
        h("td", {}, h("b", {}, w.week_label), h("div", { class: "window", style: "color:var(--meta);font-size:13px" }, w.week)),
        h("td", {}, formatWindow(w.window)),
        h("td", { class: "right num" }, count(w.review_count)),
        h("td", {}, h("span", { class: "chip" }, `${w.top_theme.label} · ${pct(w.top_theme.share)}`)),
        h("td", { class: "right" }, h("a", { class: "open-link", href: weekHref(w) }, "Open pulse", icon("arrow"))))))));
}

// ---------------------------------------------------------------- boot
document.addEventListener("DOMContentLoaded", () => {
  const page = document.body.dataset.page;
  renderShell(page);
  const root = document.getElementById("content");
  if (page === "dashboard") renderDashboard(root);
  else if (page === "weeks") renderWeeks(root);
  else if (page === "how") renderHowIcons();
});

// How it works is static HTML; this only swaps the icon placeholders for SVGs.
function renderHowIcons() {
  for (const el of document.querySelectorAll("[data-icon]")) el.replaceChildren(icon(el.dataset.icon));
}
