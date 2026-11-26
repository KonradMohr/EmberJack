(function () {
  var saved = localStorage.getItem("rj-theme") || "dark";
  window._rjTheme = saved;
  window._rjSetTheme = function (n) {
    localStorage.setItem("rj-theme", n);
    location.reload();
  };

  var r = document.documentElement;

  if (saved !== "light") {
    // Obsidian Ember — eye-friendly matte obsidian & warm burnt orange palette
    r.style.colorScheme = "dark";
    var darkVars = {
      "--bg": "#0e0e10",
      "--bg2": "#16161a",
      "--bg3": "#1f1e22",
      "--panel": "#16161a",
      "--card": "#16161a",
      "--border": "#2b2826",
      "--accent": "#e07a2f",
      "--green": "#5eb87a",
      "--gdim": "#b85e24",
      "--orange": "#f2994a",
      "--yellow": "#e6a845",
      "--red": "#d95b55",
      "--blue": "#5b9bd5",
      "--cyan": "#e07a2f",
      "--dim": "#6b645e",
      "--text": "#e6dfd7",
      "--text-dim": "#9b928a",
      "--topbar": "#16161a",
      "--topbar2": "#111114",
      "--input-bg": "#111114",
      "--input-border": "#3c322c",
      "--glow-accent": "0 0 10px rgba(224, 122, 47, 0.22)",
      "--glow-green": "0 0 10px rgba(94, 184, 122, 0.18)",
      "--rj-bg-0": "#0e0e10",
      "--rj-bg-1": "#131316",
      "--rj-bg-2": "#1e1610",
      "--rj-surface": "rgba(22, 22, 26, 0.82)",
      "--rj-surface-strong": "rgba(31, 30, 34, 0.92)",
      "--rj-border": "rgba(184, 94, 36, 0.24)",
      "--rj-border-strong": "rgba(224, 122, 47, 0.44)",
      "--rj-text": "#e6dfd7",
      "--rj-text-muted": "#9b928a",
      "--rj-accent": "#e07a2f",
      "--rj-accent-soft": "rgba(224, 122, 47, 0.16)",
      "--rj-danger": "#d95b55",
      "--rj-shadow": "0 14px 30px rgba(0, 0, 0, 0.45)",
      "--rj-glow": "0 0 18px rgba(224, 122, 47, 0.16)"
    };
    for (var dk in darkVars) r.style.setProperty(dk, darkVars[dk]);

    var ds = document.createElement("style");
    ds.id = "rj-ember-dark";
    ds.textContent =
      "body{background:radial-gradient(1200px 800px at 50% -10%,#1e1610 0%,#131316 52%,#0e0e10 100%)!important;color:#e6dfd7!important}" +
      "#topbar,[id*=topbar],header{background:#16161a!important;border-color:#2b2826!important}" +
      "#topbar h1,.top-hostname{color:#f2994a!important}" +
      "#topbar a,.back-link{color:#9b928a!important}#topbar a:hover,.back-link:hover{color:#f2994a!important}" +
      "#panel,#pLeft,#pRight,#rightPanel,.tab-content,.sub-pane,.card,.sat-card,.dev-card,.kpi-card,.stat-card,.ism-dev{background:#16161a!important;border-color:#2b2826!important}" +
      "#bbar,#radioFreqBar,#pToggle,#skyHeader,#ismHeader,#cardsFilters,#skyFilters,#tabBar,#tabRow,#mainTabs,#signalHeader,#infoTabs,#ismBottomTabs{background:#141418!important;border-color:#2b2826!important}" +
      ".mode-btn,.tb,.pill,.tune-btn,.fm-btn,.tab-btn,.main-tab,.cf,.c-chip,.live-btn,#topbar button,#topbar select,#toggleBtn{background:#1f1e22!important;border-color:#35302c!important;color:#e6dfd7!important}" +
      ".mode-btn:hover,.tb:hover,.pill:hover,.tab-btn:hover,.main-tab:hover,#topbar button:hover{background:#2d1d12!important;border-color:#e07a2f!important;color:#f5efe8!important}" +
      ".mode-btn.active,.tb.on,.pill.on,.tab-btn.on,.main-tab.on,.cf.on,.c-chip.on,.live-btn.on,#topbar button.active{border-color:#e07a2f!important;color:#f2994a!important;background:#2d1d12!important}" +
      ".text-emerald-300,.text-emerald-400{color:#f2994a!important}" +
      ".bg-emerald-500\\/10,.bg-emerald-500\\/20,.bg-emerald-500\\/8,.bg-emerald-500\\/5{background-color:rgba(224,122,47,0.16)!important}" +
      ".bg-emerald-600\\/80,.bg-emerald-500\\/80{background-color:rgba(224,122,47,0.82)!important}" +
      ".border-emerald-400\\/30,.border-emerald-400\\/40,.border-emerald-300\\/30,.border-emerald-500\\/20,.border-emerald-500\\/15{border-color:rgba(224,122,47,0.42)!important}" +
      ".bg-slate-950\\/60,.bg-slate-950\\/50,.bg-slate-950\\/45,.bg-slate-950\\/40,.bg-slate-950\\/90,.bg-slate-950\\/95{background-color:rgba(17,17,20,0.92)!important}" +
      ".bg-slate-900\\/40,.bg-slate-900\\/60,.bg-slate-900\\/70,.bg-slate-900\\/80{background-color:rgba(22,22,26,0.88)!important}" +
      ".bg-slate-800\\/40,.bg-slate-800\\/60,.bg-slate-800\\/70,.bg-slate-800\\/80{background-color:rgba(31,30,34,0.84)!important}" +
      ".border-slate-800\\/70,.border-slate-800\\/80,.border-slate-700\\/60,.border-slate-700\\/70,.border-slate-600\\/40,.border-slate-600\\/60{border-color:rgba(184,94,36,0.24)!important}" +
      ".text-slate-100,.text-slate-200{color:#f5efe8!important}.text-slate-300{color:#e6dfd7!important}.text-slate-400{color:#9b928a!important}.text-slate-500{color:#6b645e!important}" +
      ".accent-emerald-500{accent-color:#e07a2f!important}" +
      ".status-tone-ok{color:#5eb87a!important;text-shadow:none!important}" +
      ".status-tone-warn{color:#e6a845!important;text-shadow:none!important}" +
      ".status-tone-bad{color:#d95b55!important;text-shadow:none!important}" +
      ".payload-status-dot.running{background:#e07a2f!important;box-shadow:0 0 8px rgba(224,122,47,0.65)!important}" +
      "#sidebar nav>.nav-active::before{background:linear-gradient(180deg,#f2994a,#e07a2f)!important;box-shadow:0 0 10px rgba(224,122,47,0.45)!important}" +
      "button:focus-visible,a:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid #e07a2f!important;outline-offset:1px}" +
      "::-webkit-scrollbar{width:8px;height:8px}::-webkit-scrollbar-track{background:#111114}::-webkit-scrollbar-thumb{background:#3c3028;border-radius:6px}::-webkit-scrollbar-thumb:hover{background:#b85e24}";
    document.head.appendChild(ds);
    return;
  }

  // Warm Parchment & Ember Light Mode
  r.style.colorScheme = "light";
  var v = {
    "--bg": "#f7f3ee",
    "--bg2": "#ffffff",
    "--bg3": "#f2ece4",
    "--dim": "#786e65",
    "--gdim": "#b85e24",
    "--blue": "#2b6cb0",
    "--text": "#1d1916",
    "--panel": "#fcfaf7",
    "--border": "#d8cec4",
    "--accent": "#c25d1a",
    "--green": "#2b7a4b",
    "--red": "#b8322e",
    "--orange": "#c25d1a",
    "--yellow": "#a16207",
    "--card": "#ffffff",
    "--text-dim": "#5c534b",
    "--topbar": "#ede6dd",
    "--topbar2": "#e5dcd1",
    "--input-bg": "#ffffff",
    "--input-border": "#cbbeb2",
    "--gps": "#2b6cb0",
    "--glo": "#c25d1a",
    "--gal": "#2b7a4b",
    "--bds": "#a16207",
    "--rj-bg-0": "#f7f3ee",
    "--rj-bg-1": "#f1ebe3",
    "--rj-bg-2": "#e9dfd3",
    "--rj-surface": "rgba(255,255,255,.88)",
    "--rj-surface-strong": "rgba(255,255,255,.96)",
    "--rj-border": "rgba(120,80,45,.18)",
    "--rj-border-strong": "rgba(194,93,26,.38)",
    "--rj-text": "#1d1916",
    "--rj-text-muted": "#5c534b",
    "--rj-accent": "#c25d1a",
    "--rj-accent-soft": "rgba(194,93,26,.12)",
    "--rj-danger": "#b8322e",
    "--rj-shadow": "0 2px 10px rgba(45,29,18,.08)",
    "--rj-glow": "none"
  };
  for (var k in v) r.style.setProperty(k, v[k]);

  var s = document.createElement("style");
  s.textContent =
    "body{background:var(--rj-bg-0)!important;color:#1d1916!important}" +
    "#sidebar,aside{background:#ede6dd!important}" +
    "#sidebar nav>button,#sidebar nav>a,nav button[type=button],nav a.block{background:#fff!important;border-color:#d8cec4!important;color:#1d1916!important}" +
    "#sidebar nav>button:hover,#sidebar nav>a:hover{background:#f7efe7!important;border-color:#c25d1a!important}" +
    "#topbar,[id*=topbar],header{background:#ede6dd!important;border-color:#d8cec4!important}" +
    "#topbar h1,#topbar .text-emerald-300,#topbar .text-emerald-400{color:#c25d1a!important}" +
    "#topbar a,.text-slate-400,.text-slate-500{color:#5c534b!important}" +
    "#topbar span,.text-slate-200,.text-slate-300,.text-slate-100{color:#1d1916!important}" +
    "#logoutBtn{background:#fff!important;border-color:#d8cec4!important;color:#332b24!important}" +
    "#payloadSearch{background:#fff!important;border-color:#cbbeb2!important;color:#1d1916!important}" +
    "#payloadSidebar>div{background:#fff!important;border-color:#d8cec4!important}" +
    "#payloadSidebar button{color:#2d251e!important}" +
    ".text-emerald-300,.text-emerald-400{color:#c25d1a!important}" +
    "#deviceTab .terminal-wrap{border-color:#d8cec4!important;background:#111114!important;color:#e6dfd7!important}" +
    ".terminal-wrap *,.xterm *{color:inherit!important;background:inherit!important}" +
    "#settingsTab>div,#lootTab>div{background:rgba(255,255,255,.9)!important;border-color:#d8cec4!important}" +
    ".rounded-3xl{background:rgba(255,255,255,.9)!important;border-color:#d8cec4!important}" +
    ".device-shell{border-color:#d8cec4!important}" +
    "input[type=password],input[type=text]{background:#fff!important;border-color:#cbbeb2!important;color:#1d1916!important}" +
    ".payload-status-dot.running{background:#c25d1a!important;box-shadow:0 0 6px rgba(194,93,26,.3)!important}" +
    ".mode-btn,.tb,.pill,.tune-btn,.fm-btn,.tab-btn,.main-tab,.cf,.c-chip,.live-btn,.status-badge,.fix-badge{background:#fff!important;border-color:#cbbeb2!important;color:#2d251e!important}" +
    ".mode-btn:hover,.tb:hover,.pill:hover,.tab-btn:hover,.main-tab:hover{background:#f7efe7!important}" +
    ".mode-btn.active,.tb.on,.pill.on,.tab-btn.on,.main-tab.on,.cf.on,.c-chip.on,.live-btn.on{border-color:#c25d1a!important;color:#c25d1a!important;background:#fdf1e7!important}" +
    ".fix-3d{background:#e8f7ee!important;border-color:#2b7a4b!important;color:#2b7a4b!important}" +
    ".fix-no{background:#fdecec!important;border-color:#b8322e!important;color:#b8322e!important}" +
    ".status-waiting{background:#fdf6e3!important;color:#a16207!important;border-color:#d4a017!important}" +
    ".status-capturing{background:#fdecec!important;color:#b8322e!important;border-color:#d95b55!important}" +
    ".card,.sat-card,.dev-card,.kpi-card,.stat-card,.ism-dev{background:#fff!important;border-color:#d8cec4!important}" +
    "#panel,#pLeft,#pRight,#rightPanel,.tab-content,.sub-pane{background:#fcfaf7!important;border-color:#d8cec4!important}" +
    "#bbar,#radioFreqBar,#pToggle,#skyHeader,#ismHeader,#cardsFilters,#skyFilters,#tabBar,#tabRow,#mainTabs,#signalHeader,#infoTabs,#ismBottomTabs{background:#ede6dd!important;border-color:#d8cec4!important}" +
    ".card-t,.stats-title,.fg,.countdown{color:#c25d1a!important}" +
    "#overlay{background:rgba(247,243,238,.95)!important}" +
    "#overlay .msg{color:#5c534b!important}" +
    ".leaflet-container,.leaflet-tile-pane{background:#ede6dd!important}" +
    ".sat-label{background:#fff!important;border-color:#d8cec4!important;color:#1d1916!important}" +
    ".log-line,.pass-row,.stat-row,.const-row,.info-row,.sig-row,.dop-row,.sat-row{border-color:#e6ddd2!important}" +
    ".pass-row:hover,.ap:hover{background:#f7efe7!important}" +
    ".pass-row.next{background:#fdf1e7!important;border-left-color:#c25d1a!important}" +
    ".gallery-item{border-color:#d8cec4!important;background:#fff!important}" +
    "#ismLog,.dev-details,#ismConsole,#ismExport{background:#f7f3ee!important}" +
    "#toggleBtn,#legend,#statsOverlay,#apDetail{background:#fff!important;border-color:#d8cec4!important;color:#2d251e!important}" +
    "#searchBox,#filterBar .fb,#filterBar select{background:#fff!important;border-color:#cbbeb2!important;color:#2d251e!important}" +
    ".ap .name{color:#2d251e!important}" +
    "input[type=number],select{background:#fff!important;border-color:#cbbeb2!important;color:#1d1916!important}" +
    "::-webkit-scrollbar-thumb{background:#cbbeb2!important}";
  document.head.appendChild(s);

  function forceLight() {
    document
      .querySelectorAll('[class*="bg-slate-9"],[class*="bg-slate-8"],[class*="bg-slate-950"]')
      .forEach(function (el) {
        if (!el.closest(".screen-frame") && !el.closest(".terminal-wrap"))
          el.style.setProperty("background", "#fff", "important");
      });
    document.querySelectorAll('[class*="border-slate"]').forEach(function (el) {
      el.style.setProperty("border-color", "#d8cec4", "important");
    });
    document
      .querySelectorAll('[class*="text-slate-1"],[class*="text-slate-2"],[class*="text-slate-3"]')
      .forEach(function (el) {
        el.style.setProperty("color", "#1d1916", "important");
      });
    document
      .querySelectorAll('[class*="text-slate-4"],[class*="text-slate-5"]')
      .forEach(function (el) {
        el.style.setProperty("color", "#5c534b", "important");
      });
    document
      .querySelectorAll("#settingsTab .rounded-xl,#lootTab .rounded-xl")
      .forEach(function (el) {
        el.style.setProperty("background", "#fff", "important");
        el.style.setProperty("border-color", "#d8cec4", "important");
      });
    document
      .querySelectorAll("#settingsTab button[data-theme],#settingsTab button[type=button]")
      .forEach(function (el) {
        el.style.setProperty("background", "#f7f3ee", "important");
        el.style.setProperty("border-color", "#cbbeb2", "important");
        el.style.setProperty("color", "#2d251e", "important");
      });
  }
  if (document.readyState === "loading")
    document.addEventListener("DOMContentLoaded", forceLight);
  else forceLight();
  setTimeout(forceLight, 1000);
  setTimeout(forceLight, 3000);
})();
