"use strict";
/* SignalShift.ai single-page shell: demo sign-in, hash routing with in-app back/forward, theme, popovers.
   Routes: #/login · #/owner/<overview|alerts|inventory|sales|campaigns|savings|ask> · #/customer/<ads|hero|core|accessory|slow|all>
   Sign-in is a client-side demo switch between the owner and customer views; it is not authentication. */
(() => {
  const $ = (s) => document.querySelector(s);
  const ROUTES = {
    owner: ["overview", "alerts", "inventory", "sales", "campaigns", "savings", "ask"],
    customer: ["ads", "hero", "core", "accessory", "slow", "all"],
  };
  const ACCOUNTS = {
    owner: { email: "owner@solenest.in", name: "Phani Raj B.N.", role: "owner", roleLabel: "Owner · Solenest" },
    customer: { email: "customer@demo.in", name: "Prerana Sulibhavi", role: "customer", roleLabel: "Customer" },
  };
  const DEMO_PASSWORD = "demo123";
  const KEY_SESSION = "signalshift.session", KEY_THEME = "signalshift.theme", KEY_MAXIDX = "signalshift.maxidx";

  const store = {
    get(k, s = sessionStorage) { try { return JSON.parse(s.getItem(k)); } catch (e) { return null; } },
    set(k, v, s = sessionStorage) { try { v === null ? s.removeItem(k) : s.setItem(k, JSON.stringify(v)); } catch (e) { /* storage blocked */ } },
  };
  let memSession = null;  // fallback when sessionStorage is unavailable
  const getSession = () => store.get(KEY_SESSION) || memSession;
  const setSession = (s) => { memSession = s; store.set(KEY_SESSION, s); };

  /* ---------------- history: back / forward ---------------- */
  let idx = (history.state && history.state.idx) || 0;
  let maxIdx = Math.max(idx, Number(store.get(KEY_MAXIDX)) || 0);
  if (!history.state || history.state.idx === undefined) history.replaceState({ idx }, "");

  function updateHistButtons() {
    $("#btn-back").disabled = idx <= 0;
    $("#btn-fwd").disabled = idx >= maxIdx;
  }
  function navigate(hash, { replace = false } = {}) {
    if (location.hash === hash) { render(); return; }
    if (replace) history.replaceState({ idx }, "", hash);
    else { idx += 1; maxIdx = idx; store.set(KEY_MAXIDX, maxIdx); history.pushState({ idx }, "", hash); }
    render();
  }
  window.addEventListener("popstate", (e) => {
    if (e.state && typeof e.state.idx === "number") idx = e.state.idx;
    else { idx += 1; maxIdx = idx; history.replaceState({ idx }, ""); }  // a hash typed into the address bar
    render();
  });

  /* ---------------- routing ---------------- */
  function parse() {
    const m = location.hash.match(/^#\/(\w+)(?:\/(\w+))?/);
    return { area: m ? m[1] : "", page: m && m[2] ? m[2] : "" };
  }
  const defaultRoute = (role) => `#/${role}/${ROUTES[role][0]}`;

  function render() {
    const sess = getSession();
    const { area, page } = parse();
    closePopovers();
    document.querySelector(".shell").classList.remove("nav-open");
    if (!sess) {
      if (area !== "login") history.replaceState({ idx }, "", "#/login");
      showLogin();
      return;
    }
    if (area !== sess.role || !ROUTES[sess.role].includes(page)) {
      history.replaceState({ idx }, "", defaultRoute(sess.role));
      return render();
    }
    showApp(sess, page);
    updateHistButtons();
  }

  function showLogin() {
    $("#app-shell").hidden = true;
    $("#login-view").hidden = false;
    document.title = "Sign in · SignalShift.ai";
  }
  // the old /store link lands on the customer side of the sign-in form
  if (location.pathname.startsWith("/store")) document.querySelector('input[name="role"][value="customer"]').checked = true;

  let started = false;
  function showApp(sess, page) {
    $("#login-view").hidden = true;
    $("#app-shell").hidden = false;
    document.body.classList.toggle("role-owner", sess.role === "owner");
    document.body.classList.toggle("role-customer", sess.role === "customer");
    if (!started) {
      started = true;
      const acct = ACCOUNTS[sess.role];
      const initials = acct.name.split(" ").map((w) => w[0]).join("").slice(0, 2);
      $("#user-avatar").textContent = initials;
      $("#user-name").textContent = acct.name;
      $("#user-role").textContent = acct.roleLabel;
      $("#user-menu-name").textContent = acct.name;
      $("#user-menu-email").textContent = acct.email;
      $("#btn-switch-role").textContent = sess.role === "owner" ? "Switch to customer view" : "Switch to owner view";
      $("#welcome-name").textContent = acct.name.split(" ")[0];
      $("#cust-name").textContent = acct.name.split(" ")[0];
      (sess.role === "owner" ? window.OwnerApp : window.StoreApp).start();
    }
    document.querySelectorAll(".side-nav a").forEach((a) => a.classList.toggle("active", a.dataset.nav === page));
    if (sess.role === "owner") {
      document.querySelectorAll('.view[data-view]').forEach((v) => v.classList.toggle("active", v.dataset.view === page));
      const label = document.querySelector(`.side-nav a[data-nav="${page}"] span`).textContent;
      document.title = `${label} · SignalShift.ai`;
      window.OwnerApp.onShow(page);
    } else {
      const view = page === "ads" ? "ads" : "store";
      document.querySelectorAll('.view[data-view]').forEach((v) => v.classList.toggle("active", v.dataset.view === view));
      if (view === "ads") window.StoreApp.showAds();
      else window.StoreApp.setGroup(page);
      document.title = view === "ads" ? "Ads for you · SignalShift.ai" : "Solenest store · SignalShift.ai";
    }
    window.scrollTo({ top: 0 });
  }

  /* ---------------- sign in / out ---------------- */
  function signIn(role, email) {
    setSession({ role, email, at: Date.now() });
    // a reload starts exactly one live stream for the chosen role
    history.replaceState({ idx }, "", defaultRoute(role));
    location.reload();
  }
  function signOut() {
    setSession(null);
    history.replaceState({ idx }, "", "#/login");
    location.reload();
  }

  $("#login-form").addEventListener("submit", (e) => {
    e.preventDefault();
    const role = document.querySelector('input[name="role"]:checked').value;
    const email = $("#login-email").value.trim().toLowerCase();
    const pass = $("#login-pass").value;
    const err = $("#login-error");
    if (!email || !pass) { err.textContent = "Enter your email and password."; return; }
    const acct = ACCOUNTS[role];
    if (email !== acct.email || pass !== DEMO_PASSWORD) {
      err.textContent = `For the ${role} demo, use ${acct.email} / ${DEMO_PASSWORD}.`;
      return;
    }
    err.textContent = "";
    signIn(role, email);
  });
  document.querySelectorAll("[data-fill]").forEach((b) => b.addEventListener("click", () => {
    const role = b.dataset.fill;
    document.querySelector(`input[name="role"][value="${role}"]`).checked = true;
    $("#login-email").value = ACCOUNTS[role].email;
    $("#login-pass").value = DEMO_PASSWORD;
    $("#login-error").textContent = "";
  }));
  $("#btn-logout").addEventListener("click", signOut);
  $("#btn-logout-2").addEventListener("click", signOut);
  $("#btn-switch-role").addEventListener("click", () => {
    const sess = getSession();
    const role = sess && sess.role === "owner" ? "customer" : "owner";
    signIn(role, ACCOUNTS[role].email);
  });

  /* ---------------- in-app links: never a new tab, always through the router ---------------- */
  document.addEventListener("click", (e) => {
    const a = e.target.closest('a[href^="#/"]');
    if (a) { e.preventDefault(); navigate(a.getAttribute("href")); return; }
    const go = e.target.closest("[data-go]");
    if (go) navigate(go.dataset.go);
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") { closePopovers(); document.querySelector(".shell").classList.remove("nav-open"); }
    const go = e.target.closest && e.target.closest("[data-go]");
    if (go && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); navigate(go.dataset.go); }
  });
  document.querySelectorAll("[data-proxy]").forEach((b) => b.addEventListener("click", () => { closePopovers(); $(b.dataset.proxy).click(); }));
  $("#btn-back").addEventListener("click", () => history.back());
  $("#btn-fwd").addEventListener("click", () => history.forward());
  $("#btn-menu").addEventListener("click", () => document.querySelector(".shell").classList.toggle("nav-open"));
  $("#scrim").addEventListener("click", () => document.querySelector(".shell").classList.remove("nav-open"));

  /* ---------------- popovers (notifications, user menu) ---------------- */
  const POPS = [["#btn-bell", "#notif-panel"], ["#btn-user", "#user-menu"]];
  function closePopovers() {
    POPS.forEach(([b, p]) => { $(p).hidden = true; $(b).setAttribute("aria-expanded", "false"); });
  }
  POPS.forEach(([b, p]) => $(b).addEventListener("click", (e) => {
    e.stopPropagation();
    const open = $(p).hidden;
    closePopovers();
    $(p).hidden = !open;
    $(b).setAttribute("aria-expanded", String(open));
  }));
  document.addEventListener("click", (e) => { if (!e.target.closest(".popover")) closePopovers(); });

  /* ---------------- theme ---------------- */
  function effectiveTheme() {
    const t = document.documentElement.dataset.theme;
    return t || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  document.querySelectorAll(".theme-toggle").forEach((b) => b.addEventListener("click", () => {
    const next = effectiveTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem(KEY_THEME, next); } catch (e) { /* storage blocked */ }
    document.dispatchEvent(new Event("themechange"));
  }));

  /* ---------------- confirm dialog (replaces window.confirm) ---------------- */
  function confirmDialog(title, msg, okLabel = "Confirm") {
    const dlg = $("#confirm-dlg");
    $("#confirm-title").textContent = title;
    $("#confirm-msg").textContent = msg;
    $("#confirm-ok").textContent = okLabel;
    if (typeof dlg.showModal !== "function") return Promise.resolve(window.confirm(`${title}\n${msg}`));
    return new Promise((resolve) => {
      dlg.returnValue = "";
      dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
      dlg.showModal();
    });
  }

  window.Shell = { navigate, confirm: confirmDialog, closePopovers };
  render();
})();
