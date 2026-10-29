/* Verticlip — shared front end: API helper, app shell (sidebar + usage), toasts and formatting. */
(() => {
  const $ = (s, root = document) => root.querySelector(s);
  const $$ = (s, root = document) => [...root.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const ICON = {
    logo: `<svg viewBox="0 0 32 32" aria-hidden="true"><defs><linearGradient id="vcg" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#d4ff3a"/><stop offset="1" stop-color="#8b7cff"/></linearGradient></defs><rect x="9" y="2" width="14" height="28" rx="5" fill="none" stroke="url(#vcg)" stroke-width="2.8"/><path d="M14 11l6 5-6 5z" fill="url(#vcg)"/></svg>`,
    plus: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>`,
    grid: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><rect x="3" y="3" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="3" width="7.5" height="7.5" rx="2"/><rect x="3" y="13.5" width="7.5" height="7.5" rx="2"/><rect x="13.5" y="13.5" width="7.5" height="7.5" rx="2"/></svg>`,
    brush: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.5 14.5l7.8-9.6a2 2 0 0 1 3 2.6l-9.4 8"/><path d="M8 14c-2.5 0-4 1.7-4 4 0 1-.6 2-2 2 2 1.5 7 1.5 8.4-1.4.8-1.6-.4-4.6-2.4-4.6z"/></svg>`,
    link: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/></svg>`,
    card: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2.5" y="5" width="19" height="14" rx="3"/><path d="M2.5 10h19M6.5 15h3"/></svg>`,
    gear: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 0 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 0 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 0 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 0 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>`,
    out: `<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4M16 17l5-5-5-5M21 12H9"/></svg>`,
    menu: `<svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"><path d="M4 7h16M4 12h16M4 17h16"/></svg>`,
    download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg>`,
    send: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2L11 13M22 2l-7 20-4-9-9-4z"/></svg>`,
    copy: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="9" y="9" width="12" height="12" rx="2.5"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>`,
    youtube: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M10 15.5v-7l6 3.5z"/></svg>`,
    tiktok: `<svg viewBox="0 0 24 24" fill="currentColor"><path d="M16.6 5.8A4.3 4.3 0 0 1 15.5 3h-3.1v12.4a2.6 2.6 0 1 1-2.6-2.6c.3 0 .5 0 .8.1V9.7a5.7 5.7 0 1 0 4.9 5.7V9.1a7.3 7.3 0 0 0 4.3 1.4V7.4a4.3 4.3 0 0 1-3.2-1.6z"/></svg>`,
    upload: `<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 16V4M7 9l5-5 5 5M4 16v3a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-3"/></svg>`,
  };

  async function errorText(res) {
    try { const d = await res.json(); return typeof d.detail === "string" ? d.detail : "Something went wrong."; }
    catch { return res.status === 413 ? "That file is too large." : "Something went wrong."; }
  }

  /** fetch wrapper: JSON in/out, the anti-forgery header on writes, login redirect on 401. */
  async function api(path, { method = "GET", body, form } = {}) {
    const headers = { "X-Verticlip": "1" };
    let payload;
    if (form) payload = form;
    else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, { method, headers, body: payload, credentials: "same-origin" }); }
    catch { throw new Error("Can't reach Verticlip. Check your connection."); }
    if (res.status === 401 && !path.startsWith("/api/auth/")) {
      location.href = `/login?next=${encodeURIComponent(location.pathname + location.search)}`;
      throw new Error("Please log in.");
    }
    if (!res.ok) { const e = new Error(await errorText(res)); e.status = res.status; throw e; }
    return res.json();
  }

  function toast(msg, bad = false) {
    let t = $("#toast");
    if (!t) { t = document.createElement("div"); t.id = "toast"; t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.textContent = msg; t.classList.toggle("bad", bad); t.classList.add("show");
    clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("show"), 4200);
  }

  const fmtDur = (s) => {
    s = Math.max(0, Math.round(Number(s) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
  };
  function timeAgo(epoch) {
    const d = Date.now() / 1000 - epoch;
    if (!(d >= 60)) return "just now";
    if (d < 3600) return `${Math.floor(d / 60)}m ago`;
    if (d < 86400) return `${Math.floor(d / 3600)}h ago`;
    if (d < 86400 * 7) return `${Math.floor(d / 86400)}d ago`;
    return new Date(epoch * 1000).toLocaleDateString(undefined, { month: "short", day: "numeric" });
  }
  const tier = (score) => (score >= 85 ? "hi" : score >= 70 ? "mid" : "");
  const safeNext = (n) => (n && n.startsWith("/") && !n.startsWith("//") ? n : "/dashboard");

  // ---------- app shell ----------
  const NAV = [
    ["dashboard", "/dashboard", "Projects", ICON.grid],
    ["brand", "/brand", "Brand kit", ICON.brush],
    ["accounts", "/accounts", "Social accounts", ICON.link],
    ["billing", "/billing", "Plan & billing", ICON.card],
    ["settings", "/settings", "Settings", ICON.gear],
  ];
  let mePromise = null;
  const loadMe = (fresh = false) => (mePromise && !fresh ? mePromise : (mePromise = api("/api/me")));

  function drawUsage(me) {
    const u = me.usage, box = $("#usageBox");
    if (!box) return;
    const pct = u.minutes_included ? Math.min(100, (u.minutes_used / u.minutes_included) * 100) : 0;
    box.innerHTML = `
      <div class="row"><b>${esc(u.plan_name)} plan</b><span class="muted">${u.minutes_left.toLocaleString()} min left</span></div>
      <div class="meter"><div style="width:${pct}%"></div></div>
      ${u.plan === "free" ? `<a class="btn sm" href="/billing">Upgrade</a>` : ""}`;
    const name = me.user.name || me.user.email;
    $("#whoBox").innerHTML = `<span class="avatar">${esc(name.trim()[0] || "V").toUpperCase()}</span>
      <span class="em" title="${esc(me.user.email)}">${esc(me.user.email)}</span>
      <button id="logoutBtn" title="Log out" aria-label="Log out">${ICON.out}</button>`;
    $("#logoutBtn").onclick = async () => { try { await api("/api/auth/logout", { method: "POST" }); } catch { } location.href = "/"; };
  }

  function mountShell() {
    const page = document.body.dataset.page;
    const main = $("main");
    const shell = document.createElement("div");
    shell.className = "shell";
    shell.innerHTML = `
      <aside class="side" id="side">
        <a class="brand" href="/dashboard">${ICON.logo}<span>Verticlip</span></a>
        <a class="btn new" href="/dashboard#create">${ICON.plus} New project</a>
        <nav class="nav" aria-label="Main">${NAV.map(([id, href, label, icon]) =>
          `<a href="${href}"${id === page ? ' aria-current="page"' : ""}>${icon}<span>${label}</span></a>`).join("")}</nav>
        <div class="side-foot"><div class="usage" id="usageBox"><div class="meter"><div style="width:0"></div></div></div><div class="who" id="whoBox"></div></div>
      </aside>
      <div class="col">
        <div class="mobile-bar"><a class="brand" href="/dashboard">${ICON.logo}<span>Verticlip</span></a><button class="btn quiet sm" id="menuBtn" aria-label="Menu">${ICON.menu}</button></div>
      </div>`;
    main.classList.add("main");
    main.replaceWith(shell);
    $(".col", shell).append(main);
    $("#menuBtn").onclick = (e) => { e.stopPropagation(); $("#side").classList.toggle("open"); };
    document.addEventListener("click", (e) => { if (!e.target.closest("#side")) $("#side").classList.remove("open"); });
    loadMe().then(drawUsage).catch(() => { });
  }

  window.VC = { $, $$, esc, sleep, api, toast, fmtDur, timeAgo, tier, safeNext, ICON, loadMe, drawUsage };
  if (document.body.dataset.shell === "app") mountShell();
})();
