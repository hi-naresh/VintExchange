// Vint Exchange. Reads /dashboard snapshots (polling offline, Supabase Realtime
// when connected); every mutation goes to the FastAPI service. Server text is
// rendered with textContent only, so there is no HTML string injection.
(() => {
  "use strict";

  const POLL_MS = 1500;
  const SUPABASE_JS = "https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.45.4/dist/umd/supabase.min.js";
  const $ = (id) => document.getElementById(id);

  // Risk-gate checks in the exact order app/domain/policy.py evaluates them.
  const CHECKS = [
    ["KILL_SWITCH_ON", "Kill switch is off"],
    ["MERCHANT_NOT_ALLOWED", "Supplier is on the allowed list"],
    ["OUT_OF_STOCK", "Supplier has the stock"],
    ["BELOW_FLOOR", "Price is at or above the supplier's floor"],
    ["ABOVE_REQUEST_MAX", "Total is within the order's limit"],
    ["PER_ORDER_CAP_EXCEEDED", "Total is within the per-order cap"],
    ["VELOCITY_LIMIT_EXCEEDED", "Under the orders-per-minute limit"],
    ["BUDGET_EXCEEDED", "Remaining budget covers it"],
    ["REFERENCE_PRICE_EXCEEDED", "No more than 150% of a trusted web price"],
  ];
  const FLAGS = {
    LOW_CONFIDENCE_REFERENCE: "Web price is low confidence, so it flags instead of blocking",
    SEEDED_REFERENCE: "Web price is seeded demo data",
    ABOVE_REFERENCE: "Price is above 150% of the web price (flag only)",
    NO_REFERENCE: "No web price found (flag only)",
  };
  const STATUS_TEXT = {
    requested: "Requested", quoted: "Quoting", negotiating: "Negotiating", resting: "Waiting",
    reserved: "Reserved", paid: "Filled", rejected: "Blocked", out_of_stock: "Out of stock",
    cancelled: "Cancelled",
  };
  const OPEN = new Set(["requested", "quoted", "negotiating", "resting"]);
  const PLAIN = {
    KILL_SWITCH_ON: "your agent is paused",
    MERCHANT_NOT_ALLOWED: "that supplier isn't on your list",
    OUT_OF_STOCK: "the supplier ran out of stock",
    BELOW_FLOOR: "the supplier's price was invalid",
    ABOVE_REQUEST_MAX: "it costs more than you asked to pay",
    PER_ORDER_CAP_EXCEEDED: "it costs more than your per-order limit",
    VELOCITY_LIMIT_EXCEEDED: "your agent hit its orders-per-minute limit",
    BUDGET_EXCEEDED: "it would go over your budget",
    REFERENCE_PRICE_EXCEEDED: "it's far above the price elsewhere online",
    MALFORMED_LLM_OUTPUT: "the agent's answer didn't make sense, so it was ignored",
  };

  let snapshot = null;
  let merchantNames = {};
  let selectedId = null;
  let lastStatus = new Map();
  let mandateFormFilled = false;
  let refreshing = false;
  let health = {};
  let boot = { profile: "offline", realtime: false };
  let lastSnapshotText = "";
  let lastUpdate = Date.now();
  let requestsById = new Map();
  let itemsById = new Map();

  // ------------------------------------------------------------------ helpers

  const pounds = (pence) =>
    pence === null || pence === undefined
      ? "—"
      : "£" + (pence / 100).toLocaleString("en-GB", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const toPence = (value) => Math.round(Number(value) * 100);
  const clock = (iso) => {
    const d = new Date(iso);
    return Number.isNaN(d.getTime()) ? "" : d.toLocaleTimeString("en-GB", { hour12: false });
  };
  const words = (code) => String(code || "").toLowerCase().replace(/_/g, " ").replace(/-/g, " ");
  const shortAgent = (name) => (name || "").replace(/^Buyer agent: /, "");
  const each = (r) => Math.floor(r.max_price_pence / r.quantity);
  const fee = (total) => Math.round(total * (boot.take_rate_bps || 0) / 10000);
  const ago = (iso) => {
    const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    return s < 60 ? s + "s" : Math.floor(s / 60) + "m";
  };

  function el(tag, props = {}, children = []) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(props)) {
      if (value === undefined || value === null || value === false) continue;
      if (key === "text") node.textContent = value;
      else if (key === "className") node.className = value;
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of [].concat(children)) {
      if (child !== null && child !== undefined && child !== false) node.append(child);
    }
    return node;
  }

  async function api(path, body, headers = {}) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(body),
    });
    let data = null;
    try { data = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      const reason = data && (data.reason || (Array.isArray(data.detail) ? data.detail[0].msg : data.detail));
      const error = new Error(reason || "Request failed with HTTP " + response.status);
      error.code = data && data.reason_code;
      error.requestId = data && data.request_id;
      throw error;
    }
    return data;
  }

  function setStatus(id, message, isError = false) {
    const node = $(id);
    if (!node) return;
    node.textContent = message;
    node.classList.toggle("is-error", isError);
  }

  function agentFor(requestId) {
    const r = requestsById.get(requestId);
    return r && r.agent ? r.agent : null;
  }

  // ------------------------------------------------------------------ data flow

  async function refresh() {
    if (refreshing) return;
    refreshing = true;
    try {
      const response = await fetch("/dashboard");
      if (!response.ok) throw new Error("HTTP " + response.status);
      const text = await response.text();
      $("banner").hidden = true;
      lastUpdate = Date.now();
      if (text === lastSnapshotText) return; // unchanged: keep DOM (and focus) stable
      lastSnapshotText = text;
      snapshot = JSON.parse(text);
      render(snapshot);
    } catch (error) {
      const banner = $("banner");
      banner.textContent = "Disconnected from the exchange API (" + error.message + "). Showing the last snapshot; retrying.";
      banner.hidden = false;
    } finally {
      refreshing = false;
    }
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = el("script", { src, crossorigin: "anonymous" });
      script.onload = resolve;
      script.onerror = () => reject(new Error("could not load " + src));
      document.head.append(script);
    });
  }

  async function startRealtime(bootData) {
    await loadScript(SUPABASE_JS);
    const client = window.supabase.createClient(bootData.supabase_url, bootData.supabase_publishable_key);
    const channel = client.channel("vint-board");
    for (const table of ["events", "quotes", "orders"]) {
      channel.on("postgres_changes", { event: "*", schema: "public", table }, () => refresh());
    }
    channel.subscribe();
  }

  async function start() {
    try {
      const [b, h] = await Promise.all([fetch("/bootstrap"), fetch("/health")]);
      if (b.ok) boot = await b.json();
      if (h.ok) health = await h.json();
    } catch (_) { /* fall back to polling */ }
    const agents = health.llm ? ({ grok: "Live Grok agents", replay: "Grok agents (replay)", fake: "Offline demo agents" }[health.llm.mode] || health.llm.mode) : null;
    $("profile").textContent = [agents, boot.catalog, "Payments simulated"].filter(Boolean).join(". ");
    if (boot.flash_sale) $("flash-sale").textContent = boot.flash_sale.label;
    const pct = (boot.take_rate_bps || 0) / 100;
    $("store-fee").textContent = "The exchange earns " + pct + "% of each fill. No fill, no fee.";
    $("doc-fee").textContent = pct + "%";
    for (const pre of document.querySelectorAll("pre[data-base]")) {
      pre.textContent = pre.textContent.replace("{BASE}", location.origin);
    }
    await refresh();
    if (boot.realtime && boot.supabase_url && boot.supabase_publishable_key) {
      try {
        await startRealtime(boot);
        return;
      } catch (error) {
        $("profile").textContent += ". Realtime unavailable, polling";
      }
    }
    setInterval(refresh, POLL_MS);
  }

  function render(data) {
    merchantNames = Object.fromEntries(data.merchants.map((m) => [m.id, m.name]));
    requestsById = new Map(data.requests.map((r) => [r.id, r]));
    itemsById = new Map(data.inventory.map((i) => [i.id, i]));
    const focused = document.activeElement;
    const focusKey = focused && focused.dataset ? focused.dataset.focusKey : null;
    if (focused && focused.matches && focused.matches("input, select, textarea") && focused.closest(".shop-card, .product, .demand-item")) {
      // Someone is typing in a live list: skip this repaint to keep their input.
      renderCounters(data); renderMandate(data);
      return;
    }
    renderMandate(data);
    renderShopper(data);
    renderStore(data);
    renderMarket(data);
    renderBook(data);
    renderTicket(data);
    renderCounters(data);
    renderDev(data);
    if (focusKey) {
      const again = document.querySelector('[data-focus-key="' + CSS.escape(focusKey) + '"]');
      if (again) again.focus();
    }
  }

  // ------------------------------------------------------------------ modes

  const MODES = ["arena", "shopper", "store", "market", "flow", "dev"];

  function setMode(mode, focusTab = false) {
    if (!MODES.includes(mode)) mode = "arena";
    for (const m of MODES) {
      const tab = $("tab-" + m);
      const selected = m === mode;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $("view-" + m).hidden = !selected;
      if (selected && focusTab) tab.focus();
    }
    if (location.hash !== "#" + mode) history.replaceState(null, "", "#" + mode);
  }

  for (const tab of document.querySelectorAll(".mode-tab")) {
    tab.addEventListener("click", () => setMode(tab.dataset.mode));
    tab.addEventListener("keydown", (event) => {
      const i = MODES.indexOf(tab.dataset.mode);
      if (event.key === "ArrowRight") setMode(MODES[(i + 1) % MODES.length], true);
      if (event.key === "ArrowLeft") setMode(MODES[(i + MODES.length - 1) % MODES.length], true);
    });
  }
  window.addEventListener("hashchange", () => setMode(location.hash.slice(1)));
  setMode(location.hash.slice(1));

  // ------------------------------------------------------------------ shared actions

  async function reprice(merchantId, inventoryId, pricePence, statusId) {
    setStatus(statusId, "Updating price and rechecking waiting orders…");
    try {
      const result = await api("/merchants/" + merchantId + "/reprice", { inventory_id: inventoryId, price_pence: pricePence });
      const fills = result.filled_order_ids.length;
      setStatus(statusId, fills ? "Filled " + fills + " waiting order" + (fills > 1 ? "s" : "") + "." : "Price updated. No waiting order fits yet.");
    } catch (error) {
      setStatus(statusId, error.message, true);
    }
    refresh();
  }

  async function restock(merchantId, inventoryId, quantity, statusId) {
    try {
      await api("/merchants/" + merchantId + "/restock", { inventory_id: inventoryId, quantity });
      setStatus(statusId, "Added " + quantity + " to stock.");
    } catch (error) {
      setStatus(statusId, error.message, true);
    }
    refresh();
  }

  async function cancelRequest(requestId, statusId) {
    try {
      await api("/requests/" + encodeURIComponent(requestId) + "/cancel", {});
      setStatus(statusId, "Order cancelled. Nothing was bought.");
    } catch (error) {
      setStatus(statusId, "Too late to cancel: " + error.message, true);
    }
    refresh();
  }

  async function amendRequest(requestId, body, statusId) {
    try {
      const outcome = await api("/requests/" + encodeURIComponent(requestId) + "/amend", body);
      setStatus(statusId, outcome.request.status === "paid"
        ? "Updated, and it filled straight away at the best available price."
        : "Order updated. Still waiting at your new price.");
    } catch (error) {
      setStatus(statusId, "Couldn't change it: " + error.message, true);
    }
    refresh();
  }

  async function sendRequest(text, statusId, button) {
    button.disabled = true;
    setStatus(statusId, "Your agent is asking the suppliers and negotiating…");
    try {
      const outcome = await api("/requests", { text });
      selectedId = outcome.request.id;
      setStatus(statusId, "Order " + (STATUS_TEXT[outcome.request.status] || outcome.request.status).toLowerCase() + ".");
    } catch (error) {
      if (error.requestId) selectedId = error.requestId;
      setStatus(statusId, (error.code ? "Blocked by your rules: " : "") + error.message, true);
    } finally {
      button.disabled = false;
      refresh();
    }
  }

  async function simulateMarket(button, statusId) {
    button.disabled = true;
    setStatus(statusId, "6 buyer agents are trading at once…");
    try {
      const d = await api("/simulate/market", {});
      setStatus(statusId, d.agents + " agents traded in " + d.seconds.toFixed(2) + " s: " + d.filled + " filled, " +
        d.blocked + " blocked, " + d.resting + " waiting. Spend " + pounds(d.spent_pence) + " of " + pounds(d.budget_pence) + ", never over.");
    } catch (error) {
      setStatus(statusId, error.message, true);
    } finally {
      button.disabled = false;
      refresh();
    }
  }

  for (const button of document.querySelectorAll(".sim-button")) {
    button.addEventListener("click", () => simulateMarket(button, button.dataset.status));
  }

  async function setKilled(turningOn) {
    if (turningOn && !window.confirm("Pause your agent? Every order will be blocked until you resume.")) {
      return false;
    }
    try {
      await api("/kill", { killed: turningOn });
    } catch (error) {
      setStatus("mandate-status", error.message, true);
      return false;
    }
    refresh();
    return true;
  }

  // ------------------------------------------------------------------ shopper

  function renderMandate(data) {
    const m = data.mandate;
    if (!m) return;
    const left = m.budget_pence - m.spent_pence;
    $("shop-left").textContent = pounds(left);
    const pct = Math.min(100, Math.round((m.spent_pence / m.budget_pence) * 100));
    $("spend-fill").style.width = pct + "%";
    $("spend-meter").setAttribute("aria-valuenow", String(pct));
    $("budget").textContent = pounds(m.budget_pence) + " (" + pounds(m.spent_pence) + " spent)";
    $("cap").textContent = pounds(m.max_per_order_pence);
    $("velocity").textContent = String(m.orders_per_minute);
    $("allowed").textContent = m.allowed_merchant_ids.map((id) => merchantNames[id] || id).join(", ");
    $("kill").checked = m.killed;
    $("kill-state").textContent = m.killed ? "Agent paused" : "Agent active";
    $("kill-hint").textContent = m.killed ? "Every order is blocked. Tap to resume." : "Tap to pause all buying";

    if (!mandateFormFilled) {
      const form = $("mandate-form");
      form.budget.value = (m.budget_pence / 100).toFixed(2);
      form.cap.value = (m.max_per_order_pence / 100).toFixed(2);
      form.velocity.value = m.orders_per_minute;
      $("merchant-options").replaceChildren(...data.merchants.map((merchant) =>
        el("label", {}, [
          el("input", { type: "checkbox", name: "merchant", value: merchant.id,
                        checked: m.allowed_merchant_ids.includes(merchant.id) }),
          el("span", { text: merchant.name }),
        ])));
      mandateFormFilled = true;
    }
  }

  function isMine(r) {
    // The shopper's own orders: placed here, or by their own named agent (e.g. Grok Bot).
    return !r.agent || !r.agent.startsWith("Buyer agent:");
  }

  function renderShopper(data) {
    const mine = data.requests.filter(isMine).slice(0, 12);
    $("shop-empty").hidden = mine.length > 0;
    $("shop-orders").replaceChildren(...mine.map((r) => shopCard(r, data)));
  }

  function shopCard(r, data) {
    const quotes = data.quotes.filter((q) => q.request_id === r.id);
    const shops = new Set(quotes.filter((q) => q.origin !== "reprice").map((q) => q.merchant_id)).size;
    const valid = quotes.filter((q) => q.status !== "rejected" && q.price_pence && q.origin !== "reprice");
    const best = valid.reduce((a, q) => (!a || q.price_pence < a.price_pence ? q : a), null);
    const order = data.orders.find((o) => o.request_id === r.id);
    const report = order && data.reports.find((x) => x.order_id === order.id);
    const code = (data.events.find((e) => e.request_id === r.id && e.type === "policy_rejected") || { payload: {} }).payload.code;
    const unit = r.quantity > 1 ? " each" : "";
    let message, sub = null;
    let steps = ["done", "", "", "", ""];
    if (r.status === "paid" && order) {
      const shop = (merchantNames[(itemsById.get(order.inventory_id) || {}).merchant_id] || "the supplier").replace(/\.$/, "");
      message = "Bought " + (order.quantity > 1 ? order.quantity + " for " : "for ") + pounds(order.price_pence * order.quantity) +
        (order.quantity > 1 ? " (" + pounds(order.price_pence) + " each)" : "") + " from " + shop + ".";
      sub = report && report.saved_pence ? pounds(report.saved_pence) + " less than the average offer. Payment simulated." : "Payment simulated.";
      steps = ["done", "done", "done", "done", "done"];
    } else if (r.status === "resting") {
      message = best ? "Best offer " + pounds(best.price_pence) + unit + ", above your " + pounds(each(r)) + unit + "." : "No supplier could offer it yet.";
      sub = "Your agent is waiting. It buys automatically if a supplier drops to your price.";
      steps = ["done", "done", "done", "wait", ""];
    } else if (r.status === "rejected") {
      message = "Your agent refused: " + (PLAIN[code] || "it broke one of your rules") + ".";
      sub = "Nothing was bought and no money moved.";
      steps = ["done", shops ? "done" : "", shops ? "done" : "", "stop", ""];
    } else if (r.status === "out_of_stock") {
      message = "None of your suppliers has this in stock.";
      steps = ["done", "stop", "", "", ""];
    } else if (r.status === "cancelled") {
      message = "Cancelled. Nothing was bought and no money moved.";
      steps = ["done", shops ? "done" : "", "", "", ""];
    } else {
      message = "Working on it…";
    }
    const labels = ["Understood you", shops ? "Asked " + shops + (shops === 1 ? " supplier" : " suppliers") : "Asked suppliers", "Negotiated", "Checked your rules", "Bought"];
    const chipClass = ["paid", "resting", "rejected", "out_of_stock"].includes(r.status) ? r.status : "open";
    const intent = data.events.find((e) => e.request_id === r.id && e.type === "intent_parsed");

    let actions = null;
    if (OPEN.has(r.status)) {
      const priceInput = el("input", { type: "number", min: "0.01", step: "0.01", value: (each(r) / 100).toFixed(2),
                                       "aria-label": "Max price each (£)", "data-focus-key": "price-" + r.id });
      const qtyInput = el("input", { type: "number", min: "1", step: "1", value: String(r.quantity),
                                     "aria-label": "Quantity", "data-focus-key": "qty-" + r.id });
      const save = el("button", { type: "submit", className: "button button-small", text: "Update order" });
      const cancel = el("button", { type: "button", className: "button button-quiet button-small", text: "Cancel order" });
      const form = el("form", { className: "order-edit" }, [
        el("label", {}, ["Max £ each", priceInput]),
        el("label", {}, ["Quantity", qtyInput]),
        save, cancel,
      ]);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        const qty = Math.max(1, parseInt(qtyInput.value, 10) || r.quantity);
        save.disabled = true;
        priceInput.blur(); qtyInput.blur();
        amendRequest(r.id, { quantity: qty, max_price_pence: toPence(priceInput.value) * qty }, "shop-status");
      });
      cancel.addEventListener("click", () => { cancel.disabled = true; cancelRequest(r.id, "shop-status"); });
      actions = form;
    }

    return el("li", { className: "shop-card is-" + r.status }, [
      el("div", { className: "shop-card-head" }, [
        el("h3", { text: (r.quantity > 1 ? r.quantity + " × " : "") + r.query.charAt(0).toUpperCase() + r.query.slice(1) }),
        el("span", { className: "chip chip-" + chipClass, text: STATUS_TEXT[r.status] || r.status }),
      ]),
      el("p", { className: "shop-said", text: (r.agent ? "Placed by " + shortAgent(r.agent) + ": " : "You said: ") +
        "“" + (intent ? intent.payload.text : r.query) + "”" }),
      el("p", { className: "shop-message", text: message }),
      sub ? el("p", { className: "shop-sub", text: sub }) : null,
      el("ol", { className: "progress", "aria-label": "Progress" },
        labels.map((label, i) => el("li", { className: steps[i], text: label }))),
      actions,
    ]);
  }

  $("shop-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendRequest($("shop-text").value, "shop-status", event.submitter || event.target.querySelector("button[type=submit]"));
  });

  for (const preset of document.querySelectorAll(".preset[data-text]")) {
    preset.addEventListener("click", () => {
      const target = $(preset.dataset.target || "shop-text");
      target.value = preset.dataset.text;
      target.focus();
    });
  }

  $("kill").addEventListener("change", async (event) => {
    const box = event.currentTarget;
    const turningOn = box.checked;
    const ok = await setKilled(turningOn);
    if (!ok) box.checked = !turningOn;
  });

  $("mandate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const allowed = [...form.querySelectorAll("input[name=merchant]:checked")].map((i) => i.value);
    if (!allowed.length) {
      setStatus("mandate-status", "Choose at least one supplier.", true);
      return;
    }
    try {
      await api("/mandate", {
        budget_pence: toPence(form.budget.value),
        max_per_order_pence: toPence(form.cap.value),
        allowed_merchant_ids: allowed,
        orders_per_minute: Number(form.velocity.value),
      });
      setStatus("mandate-status", "Rules saved. Spend starts at £0.00.");
    } catch (error) {
      setStatus("mandate-status", error.message, true);
    }
    refresh();
  });

  // ------------------------------------------------------------------ store

  let storeMerchant = null;

  function renderStore(data) {
    const select = $("store-merchant");
    const saleMerchant = boot.flash_sale ? boot.flash_sale.merchant_id : null;
    if (!storeMerchant) storeMerchant = data.merchants.some((x) => x.id === saleMerchant) ? saleMerchant : (data.merchants[0] || {}).id;
    if (select.options.length !== data.merchants.length) {
      select.replaceChildren(...data.merchants.map((x) => el("option", { value: x.id, text: x.name })));
    }
    select.value = storeMerchant;
    const items = data.inventory.filter((i) => i.merchant_id === storeMerchant);
    const categories = new Set(items.map((i) => i.category));
    $("flash-sale").hidden = !(boot.flash_sale && boot.flash_sale.merchant_id === storeMerchant);

    // Live demand: open bids from the order book in this store's categories.
    const waiting = data.requests.filter((r) => OPEN.has(r.status) && categories.has(r.category))
      .sort((a, b) => each(b) - each(a));
    $("store-demand-empty").hidden = waiting.length > 0;
    $("store-demand").replaceChildren(...waiting.slice(0, 12).map((r) => {
      const item = items.filter((i) => i.category === r.category && i.stock >= r.quantity)
        .sort((a, b) => a.list_price_pence - b.list_price_pence)[0];
      const unitPrice = each(r);
      const canWin = item && item.floor_price_pence <= unitPrice;
      const children = [
        el("p", {}, [el("strong", { text: r.quantity + " × " + r.query + " at " + pounds(unitPrice) + " each" })]),
        el("p", { className: "demand-gap", text: (shortAgent(r.agent) || "A shopper") + ", waiting " + ago(r.created_at) + ". " + (item
          ? "You list at " + pounds(item.list_price_pence) + (canWin ? "; sell at " + pounds(unitPrice) + " for " + pounds(unitPrice * r.quantity) + "." : "; your floor is " + pounds(item.floor_price_pence) + ", too high to match.")
          : "Not enough of your stock for this lot.") }),
      ];
      if (canWin) {
        const button = el("button", { type: "button", className: "button button-sale button-small", text: "Sell at " + pounds(unitPrice) + " each" });
        button.addEventListener("click", () => { button.disabled = true; reprice(storeMerchant, item.id, unitPrice, "store-status"); });
        children.push(button);
      }
      return el("li", { className: "demand-item" }, children);
    }));

    $("store-products").replaceChildren(...items.map((i) => {
      const input = el("input", { type: "number", min: (i.floor_price_pence / 100).toFixed(2), step: "0.01",
                                  value: (i.list_price_pence / 100).toFixed(2), "aria-label": "New price for " + i.title,
                                  "data-focus-key": "reprice-" + i.id });
      const set = el("button", { type: "submit", className: "button button-quiet button-small", text: "Set price" });
      const more = el("button", { type: "button", className: "button button-quiet button-small", text: "+100 stock" });
      const form = el("form", { className: "product-edit" }, [input, set, more]);
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        input.blur();
        reprice(storeMerchant, i.id, toPence(input.value), "store-status");
      });
      more.addEventListener("click", () => restock(storeMerchant, i.id, 100, "store-status"));
      return el("li", { className: "product" }, [
        el("div", { className: "product-top" }, [
          el("strong", { text: i.title }),
          el("span", { className: "product-meta", text: pounds(i.list_price_pence) + " each, floor " + pounds(i.floor_price_pence) + ", " + i.stock + " in stock" }),
        ]),
        form,
      ]);
    }));

    const itemIds = new Set(items.map((i) => i.id));
    const sales = data.orders.filter((o) => itemIds.has(o.inventory_id) && o.status === "paid");
    $("store-sales-empty").hidden = sales.length > 0;
    $("store-sales").replaceChildren(...sales.slice(0, 15).map((o) => {
      const item = itemsById.get(o.inventory_id) || {};
      const total = o.price_pence * o.quantity;
      const draft = data.events.find((e) => e.request_id === o.request_id && e.type === "shopify_draft_order");
      return el("li", { className: "sale" }, [
        el("strong", { text: o.quantity + " × " + (item.title || o.inventory_id) + " for " + pounds(total) }),
        el("span", { text: "Buyer: " + (shortAgent(agentFor(o.request_id)) || "Shopper") + ", " + clock(o.created_at) }),
        el("span", { text: "Exchange fee " + pounds(fee(total)) + "; you receive " + pounds(total - fee(total)) + "." }),
        el("span", { text: draft ? "Shopify draft order " + (draft.payload.draft_order_name || "") + " created" : "Payment simulated; ready to ship." }),
      ]);
    }));

    const mine = data.quotes.filter((q) => q.merchant_id === storeMerchant && q.origin !== "reprice");
    $("store-quotes").replaceChildren(
      el("div", {}, [el("dt", { text: "Quotes your agent sent" }), el("dd", { text: String(mine.length) })]),
      el("div", {}, [el("dt", { text: "Rejected by the exchange" }), el("dd", { text: String(mine.filter((q) => q.status === "rejected").length) })]),
      el("div", {}, [el("dt", { text: "Sales" }), el("dd", { text: String(sales.length) })]),
    );
  }

  $("store-merchant").addEventListener("change", (event) => {
    storeMerchant = event.currentTarget.value;
    if (snapshot) renderStore(snapshot);
  });

  $("flash-sale").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const sale = boot.flash_sale;
      await reprice(sale.merchant_id, sale.inventory_id, sale.price_pence, "store-status");
    } finally {
      button.disabled = false;
    }
  });

  // ------------------------------------------------------------------ order book (per product)

  let marketItem = null;
  let seenTrades = new Set();

  function eventCategory(e) {
    if (e.request_id && requestsById.has(e.request_id)) return requestsById.get(e.request_id).category;
    if (e.payload && e.payload.inventory_id && itemsById.has(e.payload.inventory_id)) return itemsById.get(e.payload.inventory_id).category;
    return null;
  }

  function renderMarket(data) {
    const open = data.requests.filter((r) => OPEN.has(r.status));
    const paid = data.orders.filter((o) => o.status === "paid");
    if (!marketItem || !itemsById.has(marketItem)) {
      const withBids = data.inventory.filter((i) => open.some((r) => r.category === i.category));
      marketItem = (withBids[0] || data.inventory[0] || {}).id;
    }
    const item = itemsById.get(marketItem);
    if (!item) return;

    // Product list: every product with its ask, last trade and bid count.
    $("product-list").replaceChildren(...data.inventory.map((i) => {
      const last = paid.find((o) => o.inventory_id === i.id);
      const bids = open.filter((r) => r.category === i.category).length;
      const button = el("button", { type: "button", "aria-pressed": String(i.id === marketItem) }, [
        el("strong", { text: i.title }),
        el("span", { text: (merchantNames[i.merchant_id] || "") }),
        el("span", { className: "product-line", text: "Ask " + pounds(i.list_price_pence) + (last ? ", last " + pounds(last.price_pence) : "") + ", " + bids + " bid" + (bids === 1 ? "" : "s") + (i.stock ? "" : ", sold out") }),
      ]);
      button.addEventListener("click", () => { marketItem = i.id; renderMarket(snapshot); });
      return el("li", {}, button);
    }));

    // Ladder: asks (competing suppliers in this category, best at the bottom) over bids.
    const asks = data.inventory.filter((i) => i.category === item.category && i.stock > 0)
      .sort((a, b) => b.list_price_pence - a.list_price_pence);
    const bids = open.filter((r) => r.category === item.category)
      .sort((a, b) => each(b) - each(a) || a.created_at.localeCompare(b.created_at));
    $("ladder-sub").textContent = words(item.category) + ": " + asks.length + " ask" + (asks.length === 1 ? "" : "s") + ", " + bids.length + " bid" + (bids.length === 1 ? "" : "s") + ". Selected product highlighted.";
    $("ladder-asks").replaceChildren(...asks.map((a) => el("tr", { className: "ask" + (a.id === item.id ? " mine" : "") }, [
      el("td", { text: "Ask" }),
      el("td", { className: "num", text: pounds(a.list_price_pence) }),
      el("td", { className: "num", text: String(a.stock) }),
      el("td", { text: merchantNames[a.merchant_id] || a.merchant_id }),
    ])));
    $("ladder-bids").replaceChildren(...bids.slice(0, 25).map((r) => el("tr", { className: "bid" }, [
      el("td", { text: "Bid" }),
      el("td", { className: "num", text: pounds(each(r)) }),
      el("td", { className: "num", text: String(r.quantity) }),
      el("td", { text: (shortAgent(r.agent) || "Shopper") + ", " + ago(r.created_at) }),
    ])));
    const bestAsk = asks.length ? asks[asks.length - 1].list_price_pence : null;
    const bestBid = bids.length ? each(bids[0]) : null;
    $("spread").textContent = bestAsk !== null && bestBid !== null
      ? "Spread " + pounds(Math.max(0, bestAsk - bestBid)) + " (best ask " + pounds(bestAsk) + ", best bid " + pounds(bestBid) + ")"
      : bestAsk !== null ? "No bids yet. Best ask " + pounds(bestAsk) : "No asks: sold out";

    // Activity: trades in this category plus agent moves, newest first.
    const categoryItems = new Set(data.inventory.filter((i) => i.category === item.category).map((i) => i.id));
    const trades = paid.filter((o) => categoryItems.has(o.inventory_id)).map((o) => ({
      at: o.created_at, kind: "trade", fresh: seenTrades.size > 0 && !seenTrades.has(o.id),
      text: (shortAgent(agentFor(o.request_id)) || "Shopper") + " bought " + o.quantity + " at " + pounds(o.price_pence) + " from " + (merchantNames[(itemsById.get(o.inventory_id) || {}).merchant_id] || ""),
    }));
    for (const o of paid) seenTrades.add(o.id);
    const moveTypes = { request_created: "bid", request_cancelled: "cancel", request_amended: "amend", reprice: "reprice", policy_rejected: "block", restock: "restock", limit_skip: "skip" };
    const moves = data.events.filter((e) => moveTypes[e.type] && eventCategory(e) === item.category).slice(0, 40).map((e) => {
      const r = requestsById.get(e.request_id) || {};
      const p = e.payload || {};
      const who = shortAgent(r.agent) || "Shopper";
      const text = {
        request_created: who + " bid for " + (r.quantity || "") + " at " + pounds(r.quantity ? each(r) : null),
        request_cancelled: who + " cancelled their bid",
        request_amended: who + " changed their bid to " + pounds(p.quantity ? Math.floor(p.max_price_pence / p.quantity) : null) + " for " + p.quantity,
        reprice: (merchantNames[p.merchant_id] || "Supplier") + " repriced to " + pounds(p.price_pence),
        policy_rejected: who + " blocked: " + words(p.code),
        restock: (merchantNames[p.merchant_id] || "Supplier") + " restocked +" + p.quantity,
        limit_skip: who + " skipped: " + words(p.code),
      }[e.type];
      return { at: e.created_at, kind: moveTypes[e.type], text, fresh: false };
    });
    const feed = [...trades, ...moves].sort((a, b) => b.at.localeCompare(a.at)).slice(0, 40);
    $("activity-empty").hidden = feed.length > 0;
    $("activity").replaceChildren(...feed.map((f) => el("li", { className: "act act-" + f.kind + (f.fresh ? " fresh" : "") }, [
      el("span", { className: "act-kind", text: f.kind }),
      el("span", { className: "act-text", text: f.text }),
      el("time", { text: clock(f.at) }),
    ])));

    const minuteAgo = Date.now() - 60000;
    const volume = paid.reduce((sum, o) => sum + o.price_pence * o.quantity, 0);
    const agents = new Set(data.requests.filter((r) => r.agent).map((r) => r.agent));
    $("market-stats").replaceChildren(
      el("div", {}, [el("dt", { text: "Open bids" }), el("dd", { text: String(open.length) })]),
      el("div", {}, [el("dt", { text: "Fills, last minute" }), el("dd", { text: String(paid.filter((o) => new Date(o.created_at).getTime() >= minuteAgo).length) })]),
      el("div", {}, [el("dt", { text: "Volume traded" }), el("dd", { text: pounds(volume) })]),
      el("div", {}, [el("dt", { text: "Exchange fees" }), el("dd", { text: pounds(fee(volume)) })]),
      el("div", {}, [el("dt", { text: "Agents seen" }), el("dd", { text: String(agents.size) })]),
    );
  }

  setInterval(() => {
    const stale = Date.now() - lastUpdate > 6000;
    $("live-dot").classList.toggle("is-stale", stale);
    $("live-dot").textContent = stale ? "Reconnecting" : "Live";
  }, 1000);

  // ------------------------------------------------------------------ behind the scenes

  function renderBook(data) {
    $("book-empty").hidden = data.requests.length > 0;
    if (!selectedId && data.requests.length) selectedId = data.requests[0].id;
    const next = new Map();
    $("book").replaceChildren(...data.requests.slice(0, 60).map((r) => {
      const changed = lastStatus.size > 0 && lastStatus.get(r.id) !== r.status;
      next.set(r.id, r.status);
      const chipClass = ["paid", "resting", "rejected", "out_of_stock"].includes(r.status) ? r.status : "open";
      const button = el("button", { type: "button", "data-focus-key": "book-" + r.id,
                                    "aria-current": r.id === selectedId ? "true" : "false" }, [
        el("span", { className: "book-query", text: r.quantity + " × " + r.query }),
        el("span", { className: "chip chip-" + chipClass, text: STATUS_TEXT[r.status] || r.status }),
        el("span", { className: "book-meta", text: (shortAgent(r.agent) || "Shopper") + ", " + pounds(each(r)) + " each, " + clock(r.created_at) }),
      ]);
      button.addEventListener("click", () => { selectedId = r.id; renderBook(snapshot); renderTicket(snapshot); });
      return el("li", { className: "book-item" + (changed ? " changed" : "") }, button);
    }));
    lastStatus = next;
  }

  function stage(layer, name, sub, headline, detail, children = [], state = "") {
    return el("li", { className: "stage stage-" + layer + (state ? " is-" + state : "") }, [
      el("div", { className: "stage-layer" }, [el("span", { text: name }), el("small", { text: sub })]),
      el("div", { className: "stage-body" }, [
        el("p", { className: "stage-headline", text: headline }),
        detail ? el("p", { className: "stage-detail", text: detail }) : null,
        ...[].concat(children),
      ]),
    ]);
  }

  function renderTicket(data) {
    const r = data.requests.find((x) => x.id === selectedId);
    $("ticket-empty").hidden = Boolean(r);
    $("ticket-body").hidden = !r;
    if (!r) return;

    const events = data.events.filter((e) => e.request_id === r.id).reverse();
    const find = (type) => events.filter((e) => e.type === type);
    const quotes = data.quotes.filter((q) => q.request_id === r.id).reverse();
    const order = data.orders.find((o) => o.request_id === r.id);
    const report = order && data.reports.find((rep) => rep.order_id === order.id);
    const name = (id) => merchantNames[id] || id;

    $("t-title").textContent = r.query;
    const chipClass = ["paid", "resting", "rejected", "out_of_stock"].includes(r.status) ? r.status : "open";
    const tStatus = [el("span", { className: "chip chip-" + chipClass, text: STATUS_TEXT[r.status] || r.status })];
    if (OPEN.has(r.status)) {
      const b = el("button", { type: "button", className: "button button-quiet button-small", text: "Cancel order" });
      b.addEventListener("click", () => { b.disabled = true; cancelRequest(r.id, "shop-status"); });
      tStatus.push(" ", b);
    }
    $("t-status").replaceChildren(...tStatus);

    const stages = [];

    // 1. Grok buyer agent: words to intent
    const intent = find("intent_parsed")[0];
    const chips = el("div", { className: "intent" }, [
      el("span", { text: "category: " + r.category }),
      el("span", { text: "limit: " + pounds(r.max_price_pence) }),
      el("span", { text: "quantity: " + r.quantity }),
    ]);
    stages.push(stage("edge", "Grok agents", "Buyer agent",
      intent ? (intent.payload.agent ? "Request from agent \u201C" + intent.payload.agent + "\u201D, turned into a structured order" : "Turned your words into a structured request") : "Structured request received",
      intent && intent.latency_ms >= 1 ? "Parsed in " + intent.latency_ms.toFixed(0) + " ms" : null,
      [intent ? el("p", { className: "said", text: "“" + intent.payload.text + "”" }) : null, chips]));

    // 2. Grok merchant agents: quotes and counters
    const agentQuotes = quotes.filter((q) => q.origin !== "reprice");
    const counters = find("counter_sent");
    if (agentQuotes.length) {
      const rows = agentQuotes.map((q) => el("tr", { className: q.status === "rejected" ? "q-rejected" : q.status === "accepted" ? "q-accepted" : "" }, [
        el("td", { text: name(q.merchant_id) }),
        el("td", { className: "num", text: String(q.round) }),
        el("td", { className: "num", text: pounds(q.price_pence) }),
        el("td", { className: "result", text: q.status === "rejected" ? words(q.rejection_reason) : q.status }),
      ]));
      const merchants = new Set(agentQuotes.map((q) => q.merchant_id)).size;
      stages.push(stage("edge", "Grok agents", "Merchant agents",
        merchants + " merchant agents quoted" + (counters.length ? ", buyer countered " + counters.length + "x" : ""),
        counters.length ? "Counter offers: " + counters.map((c) => pounds(c.payload.price_pence)).join(", ") : "Quoted at the same time, each with its own timeout",
        el("table", { className: "quotes" }, [
          el("thead", {}, el("tr", {}, [el("th", { text: "Merchant" }), el("th", { className: "num", text: "Round" }), el("th", { className: "num", text: "Price" }), el("th", { text: "Result" })])),
          el("tbody", {}, rows),
        ])));
    } else {
      stages.push(stage("edge", "Grok agents", "Merchant agents",
        r.status === "out_of_stock" ? "No allowed merchant had stock, so no agent was asked" : "No quotes yet", null, [], "waiting"));
    }

    // 3. Matching
    const valid = agentQuotes.filter((q) => q.status !== "rejected" && q.price_pence);
    const best = valid.reduce((a, q) => (!a || q.price_pence < a.price_pence ? q : a), null);
    const repriceQuote = quotes.find((q) => q.origin === "reprice");
    const ref = find("market_reference")[0];
    const refLine = ref && ref.payload.price_pence
      ? "Web reference " + pounds(ref.payload.price_pence) + " (" + words(ref.payload.source) + ", " + ref.payload.confidence + " confidence)"
      : "No web reference price found";
    if (repriceQuote && r.status === "paid") {
      stages.push(stage("match", "Matching", "Deterministic", "Resting order matched when " + name(repriceQuote.merchant_id) + " repriced to " + pounds(repriceQuote.price_pence),
        refLine, [], "fill"));
    } else if (r.status === "resting") {
      stages.push(stage("match", "Matching", "Deterministic",
        best ? "Best quote " + pounds(best.price_pence) + " is over your " + pounds(r.max_price_pence) + " limit, so it rests as a limit order"
             : "No valid quote, so it rests as a limit order",
        "It fills by itself if a merchant reprices within your limit. " + refLine));
    } else if (best && best.price_pence * r.quantity <= r.max_price_pence) {
      stages.push(stage("match", "Matching", "Deterministic",
        "Best quote " + pounds(best.price_pence) + " from " + name(best.merchant_id) + " fits your " + pounds(r.max_price_pence) + " limit",
        "Ranked by price, then delivery time, then merchant rating. " + refLine));
    } else if (repriceQuote) {
      stages.push(stage("match", "Matching", "Deterministic", "Matched when " + name(repriceQuote.merchant_id) + " repriced to " + pounds(repriceQuote.price_pence),
        refLine));
    } else {
      stages.push(stage("match", "Matching", "Deterministic", "Nothing to match", null, [], "waiting"));
    }

    // 4. Risk gate
    const rejected = find("policy_rejected").slice(-1)[0];
    const checked = find("policy_checked").slice(-1)[0];
    const code = rejected ? rejected.payload.code : checked ? checked.payload.code : null;
    if (code) {
      const failIndex = CHECKS.findIndex(([c]) => c === code);
      const list = CHECKS.map(([c, label], i) => {
        let cls = "pass", mark = "✓";
        if (code !== "ALLOWED") {
          if (failIndex === -1) { cls = "skip"; mark = "–"; }
          else if (i === failIndex) { cls = "fail"; mark = "✕"; }
          else if (i > failIndex) { cls = "skip"; mark = "–"; }
        }
        return el("li", { className: "check " + cls }, [el("span", { className: "check-mark", "aria-hidden": "true", text: mark }),
          el("span", { text: label + (cls === "skip" ? " (not reached)" : "") })]);
      });
      const flags = (checked && checked.payload.flags) || [];
      for (const f of flags) {
        list.push(el("li", { className: "check flag" }, [el("span", { className: "check-mark", "aria-hidden": "true", text: "!" }), el("span", { text: FLAGS[f] || words(f) })]));
      }
      const allowed = code === "ALLOWED";
      stages.push(stage("gate", "Risk gate", "Your mandate",
        allowed ? "Passed all " + CHECKS.length + " mandate checks" : "Blocked: " + (rejected.payload.reason || words(code)),
        allowed ? null : "No prompt can change these rules. Nothing moved.",
        el("ul", { className: "checks" }, list), allowed ? "fill" : "block"));
    } else {
      stages.push(stage("gate", "Risk gate", "Your mandate", "Not reached: nothing has matched yet", null, [], "waiting"));
    }

    // 5. Ledger
    if (order && order.status === "paid") {
      const facts = el("dl", { className: "facts" }, [
        el("div", {}, [el("dt", { text: "Paid" }), el("dd", { text: pounds(order.price_pence * order.quantity) })]),
        report ? el("div", {}, [el("dt", { text: "Average quote" }), el("dd", { text: pounds(report.average_quote_pence) })]) : null,
        report ? el("div", {}, [el("dt", { text: "Saved" }), el("dd", { text: pounds(report.saved_pence) })]) : null,
        report && report.web_reference_pence ? el("div", {}, [el("dt", { text: "Web price" }), el("dd", { text: pounds(report.web_reference_pence) })]) : null,
      ]);
      stages.push(stage("ledger", "Ledger", "Atomic transaction", "Stock reserved, budget committed, order filled",
        null, [facts, el("p", { className: "note" }, [el("span", { className: "sim", text: "Simulated payment" }), " One order per idempotency key; retries can't double charge."])], "fill"));
    } else if (order) {
      stages.push(stage("ledger", "Ledger", "Atomic transaction", "Order " + order.status.replace(/_/g, " ")));
    } else if (r.status === "rejected") {
      stages.push(stage("ledger", "Ledger", "Atomic transaction", "Nothing written", "No stock reserved, no budget spent.", [], "waiting"));
    } else {
      stages.push(stage("ledger", "Ledger", "Atomic transaction", "Waiting for a match that passes the risk gate", null, [], "waiting"));
    }

    // 6. Merchant store
    const draft = find("shopify_draft_order")[0];
    const draftFailed = find("shopify_draft_order_failed")[0];
    if (draft) {
      stages.push(stage("shop", "Merchant store", "Shopify", "Draft order " + (draft.payload.draft_order_name || "") + " created in the merchant's Shopify admin",
        "Carries the negotiated discount; tagged simulated-payment.", [], "fill"));
    } else if (draftFailed) {
      stages.push(stage("shop", "Merchant store", "Shopify", "Shopify didn't accept the draft order", draftFailed.payload.error + ". The fill in the ledger still stands."));
    } else if (order && order.status === "paid") {
      stages.push(stage("shop", "Merchant store", "Shopify",
        health.shopify_draft_orders ? "Creating the Shopify draft order…" : "Shopify mirror is off",
        health.shopify_draft_orders ? null : "Set SHOPIFY_ADMIN_TOKEN to land each fill as a Shopify draft order.", [], "waiting"));
    } else {
      stages.push(stage("shop", "Merchant store", "Shopify", "Nothing to send", null, [], "waiting"));
    }

    $("stages").replaceChildren(...stages);
  }


  function renderCounters(data) {
    $("c-orders").textContent = String(data.counters.orders);
    $("c-blocked").textContent = String(data.counters.blocked_attempts);
    $("c-saved").textContent = pounds(data.counters.saved_pence);
  }

  function renderLatency(data, targetId = "latency", labels = { llm: "Grok agents", policy: "Risk gate", database: "Ledger" }) {
    const worst = Math.max(1, ...Object.values(data.latency).map((s) => s.p95_ms || 0));
    $(targetId).replaceChildren(...["llm", "policy", "database"].map((stageName) => {
      const s = data.latency[stageName] || { count: 0 };
      const fmt = (v) => (v === null || v === undefined ? "—" : v < 10 ? v.toFixed(2) + " ms" : Math.round(v) + " ms");
      const width = s.p95_ms ? Math.max(2, (s.p95_ms / worst) * 100) : 0;
      return el("tr", { "data-stage": stageName }, [
        el("td", {}, [el("span", { text: labels[stageName] }), el("span", { className: "bar", style: "width:" + width + "%" })]),
        el("td", { text: fmt(s.median_ms) }),
        el("td", { text: fmt(s.p95_ms) }),
        el("td", { text: String(s.count) }),
      ]);
    }));
  }

  let devOpen = new Set();

  function renderDev(data) {
    $("dev-health").textContent = "GET /health\n" + JSON.stringify(health, null, 2);
    renderLatency(data, "dev-latency", { llm: "llm", policy: "policy", database: "database" });
    const list = $("dev-events");
    for (const d of list.querySelectorAll("details[open]")) devOpen.add(d.dataset.id);
    list.replaceChildren(...data.events.slice(0, 120).map((e) => {
      const details = el("details", { "data-id": e.id, open: devOpen.has(e.id) }, [
        el("summary", {}, [
          el("span", { text: clock(e.created_at) }),
          el("span", { className: "ev-stage ev-stage-" + e.stage, text: e.stage }),
          el("span", { className: "ev-type", text: e.type }),
          el("span", { className: "ev-req", text: e.latency_ms !== null && e.latency_ms !== undefined ? e.latency_ms.toFixed(2) + " ms" : "" }),
        ]),
        el("pre", { text: JSON.stringify({ request_id: e.request_id, ...e.payload }, null, 2) }),
      ]);
      details.addEventListener("toggle", () => {
        if (details.open) devOpen.add(e.id); else devOpen.delete(e.id);
      });
      return el("li", {}, details);
    }));
  }

  start();
})();
