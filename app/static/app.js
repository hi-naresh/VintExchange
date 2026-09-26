// Trading Agentic Commerce. Reads /dashboard snapshots (polling offline, Supabase Realtime
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
    ["MERCHANT_NOT_ALLOWED", "Merchant is on your allowed list"],
    ["OUT_OF_STOCK", "Merchant has the stock"],
    ["BELOW_FLOOR", "Price is at or above the merchant's floor"],
    ["ABOVE_REQUEST_MAX", "Total is within your request limit"],
    ["PER_ORDER_CAP_EXCEEDED", "Total is within your per-order cap"],
    ["VELOCITY_LIMIT_EXCEEDED", "Under your orders-per-minute limit"],
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
    requested: "Requested", quoted: "Quoting", negotiating: "Negotiating", resting: "Resting",
    reserved: "Reserved", paid: "Filled", rejected: "Blocked", out_of_stock: "Out of stock",
    cancelled: "Cancelled",
  };

  let snapshot = null;
  let merchantNames = {};
  let selectedId = null;
  let lastStatus = new Map();
  let mandateFormFilled = false;
  let repriceFilled = false;
  let refreshing = false;
  let health = {};
  let boot = { profile: "offline", realtime: false };
  let lastSnapshotText = "";

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
  const words = (code) => String(code || "").toLowerCase().replace(/_/g, " ");

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

  async function api(path, body) {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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
    node.textContent = message;
    node.classList.toggle("is-error", isError);
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

  async function startRealtime(boot) {
    await loadScript(SUPABASE_JS);
    const client = window.supabase.createClient(boot.supabase_url, boot.supabase_publishable_key);
    const channel = client.channel("tac-board");
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
    const fee = (boot.take_rate_bps || 0) / 100;
    $("store-fee").textContent = "The exchange earns " + fee + "% of each fill. No fill, no fee.";
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

  // ------------------------------------------------------------------ rendering

  function render(data) {
    const focusedId = document.activeElement && document.activeElement.dataset
      ? document.activeElement.dataset.requestId : null;
    renderAll(data);
    if (focusedId) {
      const again = document.querySelector('[data-request-id="' + CSS.escape(focusedId) + '"]');
      if (again) again.focus();
    }
  }

  function renderAll(data) {
    merchantNames = Object.fromEntries(data.merchants.map((m) => [m.id, m.name]));
    renderMandate(data);
    renderReprice(data);
    renderBook(data);
    renderTicket(data);
    renderCounters(data);
    renderLatency(data);
    renderEvents(data);
    renderShopper(data);
    renderStore(data);
    renderDev(data);
  }

  function renderMandate(data) {
    const m = data.mandate;
    if (!m) return;
    $("spent").textContent = pounds(m.spent_pence);
    $("budget").textContent = pounds(m.budget_pence);
    const pct = Math.min(100, Math.round((m.spent_pence / m.budget_pence) * 100));
    $("spend-fill").style.width = pct + "%";
    $("spend-meter").setAttribute("aria-valuenow", String(pct));
    $("cap").textContent = pounds(m.max_per_order_pence);
    $("velocity").textContent = String(m.orders_per_minute);
    $("allowed").textContent = m.allowed_merchant_ids.map((id) => merchantNames[id] || id).join(", ");

    const kill = $("kill");
    kill.checked = m.killed;
    $("kill-state").textContent = m.killed ? "Kill switch on" : "Kill switch off";
    $("kill-hint").textContent = m.killed ? "The risk gate blocks every order" : "Orders can execute";

    if (!mandateFormFilled) {
      const form = $("mandate-form");
      form.budget.value = (m.budget_pence / 100).toFixed(2);
      form.cap.value = (m.max_per_order_pence / 100).toFixed(2);
      form.velocity.value = m.orders_per_minute;
      $("merchant-options").replaceChildren(...data.merchants.map((merchant) =>
        el("label", {}, [
          el("input", { type: "checkbox", name: "merchant", value: merchant.id,
                        checked: m.allowed_merchant_ids.includes(merchant.id) }),
          el("span", { text: merchant.name + " (" + merchant.rating.toFixed(1) + " rating)" }),
        ])));
      mandateFormFilled = true;
    }
  }

  function renderReprice(data) {
    const select = $("reprice-item");
    const current = select.value;
    select.replaceChildren(...data.inventory.map((item) =>
      el("option", { value: item.id, "data-merchant": item.merchant_id,
                     text: item.title + " (" + (merchantNames[item.merchant_id] || item.merchant_id) + ", now " +
                           pounds(item.list_price_pence) + ", floor " + pounds(item.floor_price_pence) + ", " + item.stock + " left)" })));
    if (current) select.value = current;
    const saleItem = boot.flash_sale ? boot.flash_sale.inventory_id : "inventory-bassline-pro";
    if (!repriceFilled && data.inventory.some((i) => i.id === saleItem)) {
      select.value = saleItem;
      repriceFilled = true;
    }
    const item = data.inventory.find((i) => i.id === select.value);
    const price = $("reprice-form").price;
    if (item && !price.value) price.value = (item.floor_price_pence / 100).toFixed(2);
  }

  function renderBook(data) {
    $("book-empty").hidden = data.requests.length > 0;
    if (!selectedId && data.requests.length) selectedId = data.requests[0].id;
    const next = new Map();
    $("book").replaceChildren(...data.requests.map((r) => {
      const changed = lastStatus.size > 0 && lastStatus.get(r.id) !== r.status;
      next.set(r.id, r.status);
      const chipClass = ["paid", "resting", "rejected", "out_of_stock"].includes(r.status) ? r.status : "open";
      const button = el("button", { type: "button", "data-request-id": r.id,
                                    "aria-current": r.id === selectedId ? "true" : "false" }, [
        el("span", { className: "book-query", text: r.query }),
        el("span", { className: "chip chip-" + chipClass, text: STATUS_TEXT[r.status] || r.status }),
        el("span", { className: "book-meta", text: (agentFor(r.id, data.events) ? agentFor(r.id, data.events).replace(/^Buyer agent: /, "") + ", " : "") + "limit " + pounds(r.max_price_pence) + ", " + clock(r.created_at) }),
      ]);
      button.addEventListener("click", () => { selectedId = r.id; render(snapshot); });
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
      b.addEventListener("click", () => { b.disabled = true; cancelRequest(r.id, "request-status"); });
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

  function describe(event) {
    const p = event.payload || {};
    const who = merchantNames[p.merchant_id] || p.merchant_id || "";
    switch (event.type) {
      case "intent_parsed": return ["llm", "Buyer agent parsed: " + p.query + ", limit " + pounds(p.max_price_pence)];
      case "request_created": return ["neutral", "Request opened: " + p.query];
      case "market_reference": return ["neutral", p.price_pence ? "Web reference " + pounds(p.price_pence) + " (" + words(p.source) + ")" : "No web reference found"];
      case "merchant_quote": return ["llm", who + " agent replied, round " + p.round + (p.ok ? "" : ", unusable")];
      case "buyer_counter": return ["llm", "Buyer agent weighed round " + p.round];
      case "quote_received": return ["neutral", who + " quoted " + pounds(p.price_pence)];
      case "quote_rejected": return ["block", who + " quote rejected", p.code];
      case "counter_sent": return ["neutral", "Buyer countered at " + pounds(p.price_pence)];
      case "buyer_walked": return ["neutral", "Buyer stopped negotiating"];
      case "negotiation_stopped": return ["neutral", "Negotiation stopped", p.code];
      case "request_resting": return ["neutral", "Resting at limit " + pounds(p.max_price_pence)];
      case "policy_checked": return ["neutral", "Risk gate checked", p.code];
      case "policy_rejected": return ["block", p.reason || "Blocked by the risk gate", p.code];
      case "order_reserved": return ["neutral", "Ledger reserved stock"];
      case "order_paid": return ["fill", "Filled for " + pounds(p.paid_pence) + ", simulated payment"];
      case "payment_failed": return ["block", "Simulated payment failed; stock and budget restored"];
      case "reprice": return ["neutral", who + " repriced to " + pounds(p.price_pence)];
      case "limit_fill": return ["fill", "Resting order filled at " + pounds(p.price_pence)];
      case "limit_skip": return ["neutral", "Resting order skipped", p.code];
      case "kill_switch": return [p.killed ? "block" : "neutral", p.killed ? "Kill switch turned on" : "Kill switch turned off"];
      case "mandate_set": return ["neutral", "Mandate set: budget " + pounds(p.budget_pence) + ", cap " + pounds(p.max_per_order_pence)];
      case "out_of_stock": return ["block", "No stock in " + p.category, p.code];
      case "request_cancelled": return ["neutral", "Order cancelled" + (p.agent ? " by " + p.agent : "")];
      case "cancel_rejected": return ["block", "Cancel refused: already " + (p.status || "closed"), p.code];
      case "simulation_started": return ["llm", p.agents + " buyer agents entered the market at once"];
      case "simulation_finished": return ["fill", "Busy market done: " + p.filled + " filled, " + p.blocked + " blocked, " + p.resting + " waiting"];
      case "shopify_draft_order": return ["fill", "Shopify draft order " + (p.draft_order_name || "") + " created"];
      case "shopify_draft_order_failed": return ["neutral", "Shopify draft order not created: " + (p.error || "unknown error")];
      default: return ["neutral", words(event.type)];
    }
  }

  const MARKS = { block: "✕", fill: "✓", llm: "◆", neutral: "·" };

  function renderEvents(data) {
    $("events").replaceChildren(...data.events.slice(0, 80).map((event) => {
      const [tone, text, code] = describe(event);
      const latency = event.latency_ms !== null && event.latency_ms !== undefined ? " (" + event.latency_ms.toFixed(1) + " ms)" : "";
      return el("li", { className: "event tone-" + tone }, [
        el("span", { className: "event-mark", "aria-hidden": "true", text: MARKS[tone] }),
        el("span", { className: "event-text" }, [
          code ? el("span", { className: "event-code", text: code + " " }) : null,
          el("span", { text: text + latency }),
        ]),
        el("time", { className: "event-time", datetime: event.created_at, text: clock(event.created_at) }),
      ]);
    }));
  }

  // ------------------------------------------------------------------ views

  const MODES = ["shopper", "store", "flow", "dev"];

  function setMode(mode, focusTab = false) {
    if (!MODES.includes(mode)) mode = "shopper";
    for (const m of MODES) {
      const tab = $("tab-" + m);
      const selected = m === mode;
      tab.setAttribute("aria-selected", String(selected));
      tab.tabIndex = selected ? 0 : -1;
      $("view-" + (m === "store" ? "store" : m)).hidden = !selected;
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

  // Plain-English reasons for the shopper view.
  const PLAIN = {
    KILL_SWITCH_ON: "your agent is paused",
    MERCHANT_NOT_ALLOWED: "that shop isn't on your list",
    OUT_OF_STOCK: "the shop ran out of stock",
    BELOW_FLOOR: "the shop's price was invalid",
    ABOVE_REQUEST_MAX: "it costs more than you asked to pay",
    PER_ORDER_CAP_EXCEEDED: "it costs more than your per-order limit",
    VELOCITY_LIMIT_EXCEEDED: "your agent hit its orders-per-minute limit",
    BUDGET_EXCEEDED: "it would go over your budget",
    REFERENCE_PRICE_EXCEEDED: "it's far above the price elsewhere online",
    MALFORMED_LLM_OUTPUT: "the agent's answer didn't make sense, so it was ignored",
  };

  function agentFor(requestId, events) {
    const hit = events.find((e) => e.request_id === requestId && e.type === "intent_parsed");
    return hit && hit.payload.agent ? hit.payload.agent : null;
  }

  const OPEN = new Set(["requested", "quoted", "negotiating", "resting"]);

  async function cancelRequest(requestId, statusId) {
    try {
      await api("/requests/" + encodeURIComponent(requestId) + "/cancel", {});
      setStatus(statusId, "Order cancelled. Nothing was bought.");
    } catch (error) {
      setStatus(statusId, "Too late to cancel: " + error.message, true);
    }
    refresh();
  }

  async function simulateMarket(button, statusId) {
    button.disabled = true;
    setStatus(statusId, "6 buyer agents are trading at once…");
    try {
      const d = await api("/simulate/market", {});
      setStatus(statusId, d.agents + " agents traded in " + d.seconds.toFixed(2) + " s: " + d.filled + " filled, " +
        d.blocked + " blocked, " + d.resting + " still waiting. Spend " + pounds(d.spent_pence) + " of " + pounds(d.budget_pence) + ", never over.");
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

  function blockCode(requestId, events) {
    const hit = events.find((e) => e.request_id === requestId && e.type === "policy_rejected");
    return hit ? hit.payload.code : null;
  }

  function renderShopper(data) {
    const m = data.mandate;
    if (m) {
      const left = m.budget_pence - m.spent_pence;
      $("shop-left").textContent = pounds(left);
      $("shop-fill").style.width = Math.max(0, Math.round((left / m.budget_pence) * 100)) + "%";
      $("shop-cap").textContent = pounds(m.max_per_order_pence);
      $("shop-allowed").textContent = m.allowed_merchant_ids.map((id) => merchantNames[id] || id).join(", ");
      $("shop-pause").textContent = m.killed ? "Resume my agent" : "Pause my agent";
    }
    $("shop-empty").hidden = data.requests.length > 0;
    $("shop-orders").replaceChildren(...data.requests.slice(0, 8).map((r) => {
      const quotes = data.quotes.filter((q) => q.request_id === r.id && q.status !== "rejected" && q.price_pence);
      const shops = new Set(data.quotes.filter((q) => q.request_id === r.id && q.origin !== "reprice").map((q) => q.merchant_id)).size;
      const best = quotes.filter((q) => q.origin !== "reprice").reduce((a, q) => (!a || q.price_pence < a.price_pence ? q : a), null);
      const order = data.orders.find((o) => o.request_id === r.id);
      const report = order && data.reports.find((x) => x.order_id === order.id);
      const intent = data.events.find((e) => e.request_id === r.id && e.type === "intent_parsed");
      const code = blockCode(r.id, data.events);
      let message, sub = null;
      let steps = ["done", "", "", "", ""];
      if (r.status === "paid" && order) {
        const shop = merchantNames[(data.inventory.find((i) => i.id === order.inventory_id) || {}).merchant_id] || "the shop";
        message = "Bought " + (order.quantity > 1 ? order.quantity + " for " : "for ") + pounds(order.price_pence * order.quantity) +
          (order.quantity > 1 ? " (" + pounds(order.price_pence) + " each)" : "") + " from " + shop.replace(/\.$/, "") + ".";
        sub = report && report.saved_pence ? "That's " + pounds(report.saved_pence) + " less than the average offer. Payment simulated." : "Payment simulated.";
        steps = ["done", "done", "done", "done", "done"];
      } else if (r.status === "resting") {
        const perUnitLimit = Math.floor(r.max_price_pence / r.quantity);
        message = best ? "Best offer is " + pounds(best.price_pence) + (r.quantity > 1 ? " each" : "") + ", above your " + pounds(perUnitLimit) + (r.quantity > 1 ? " each" : "") + "." : "No shop could offer it yet.";
        sub = "Your agent is waiting and will buy automatically if a shop drops its price.";
        steps = ["done", "done", "done", "wait", ""];
      } else if (r.status === "rejected") {
        message = "Your agent refused: " + (PLAIN[code] || "it broke one of your rules") + ".";
        sub = "Nothing was bought and no money moved.";
        steps = ["done", shops ? "done" : "", shops ? "done" : "", "stop", ""];
      } else if (r.status === "out_of_stock") {
        message = "None of your shops has this in stock.";
        steps = ["done", "stop", "", "", ""];
      } else if (r.status === "cancelled") {
        message = "Cancelled. Nothing was bought and no money moved.";
        steps = ["done", shops ? "done" : "", "", "", ""];
      } else {
        message = "Working on it…";
      }
      const labels = ["Understood you", shops ? "Asked " + shops + (shops === 1 ? " shop" : " shops") : "Asked the shops", "Negotiated", "Checked your rules", "Bought"];
      return el("li", { className: "shop-card is-" + r.status }, [
        el("div", { className: "shop-card-head" }, [
          el("h3", { text: r.query.charAt(0).toUpperCase() + r.query.slice(1) }),
          el("span", { className: "chip chip-" + (["paid", "resting", "rejected", "out_of_stock"].includes(r.status) ? r.status : "open"),
                       text: STATUS_TEXT[r.status] || r.status }),
        ]),
        intent ? el("p", { className: "shop-said", text: (intent.payload.agent ? "Placed by \u201C" + intent.payload.agent.replace(/^Buyer agent: /, "") + "\u201D: " : "You said: ") + "\u201C" + intent.payload.text + "\u201D" }) : null,
        el("p", { className: "shop-message", text: message }),
        sub ? el("p", { className: "shop-sub", text: sub }) : null,
        el("ol", { className: "progress", "aria-label": "Progress" },
          labels.map((label, i) => el("li", { className: steps[i], text: label }))),
        OPEN.has(r.status) ? (() => {
          const b = el("button", { type: "button", className: "button button-quiet button-small", text: "Cancel order" });
          b.addEventListener("click", () => { b.disabled = true; cancelRequest(r.id, "shop-status"); });
          return el("div", { className: "shop-actions" }, b);
        })() : null,
      ]);
    }));
  }

  let storeMerchant = null;

  function renderStore(data) {
    const select = $("store-merchant");
    const saleMerchant = boot.flash_sale ? boot.flash_sale.merchant_id : "merchant-bassline";
    if (!storeMerchant) storeMerchant = data.merchants.some((x) => x.id === saleMerchant) ? saleMerchant : (data.merchants[0] || {}).id;
    select.replaceChildren(...data.merchants.map((x) => el("option", { value: x.id, text: x.name })));
    select.value = storeMerchant;
    const items = data.inventory.filter((i) => i.merchant_id === storeMerchant);
    const categories = new Set(items.map((i) => i.category));

    // Demand: resting buyers in categories this store sells.
    const waiting = data.requests.filter((r) => r.status === "resting" && categories.has(r.category));
    $("store-demand-empty").hidden = waiting.length > 0;
    $("store-demand").replaceChildren(...waiting.map((r) => {
      const item = items.filter((i) => i.category === r.category && i.stock >= r.quantity)
        .sort((a, b) => a.list_price_pence - b.list_price_pence)[0];
      const unit = Math.floor(r.max_price_pence / r.quantity);
      const canWin = item && item.floor_price_pence <= unit;
      const want = r.quantity > 1
        ? r.quantity + " \u00D7 " + r.query + " at " + pounds(unit) + " each or less (" + pounds(r.max_price_pence) + " total)"
        : r.query + " at " + pounds(r.max_price_pence) + " or less";
      const each = r.quantity > 1 ? " each" : "";
      const children = [
        el("p", {}, [el("strong", { text: "A buyer's agent wants " + want + "." })]),
        el("p", { className: "demand-gap", text: item
          ? "Your " + item.title + " is " + pounds(item.list_price_pence) + each + (canWin ? ". Drop to " + pounds(unit) + each + " to win this " + pounds(unit * r.quantity) + " sale now." : ". Your floor is " + pounds(item.floor_price_pence) + ", so you can't match this one.")
          : "You don't have stock in this category." }),
      ];
      if (canWin) {
        const button = el("button", { type: "button", className: "button button-sale", text: "Sell at " + pounds(unit) + each });
        button.addEventListener("click", async () => {
          button.disabled = true;
          await reprice(storeMerchant, item.id, unit, "store-status");
        });
        children.push(button);
      }
      return el("li", { className: "demand-item" }, children);
    }));

    $("store-products").replaceChildren(...items.map((i) => el("tr", {}, [
      el("td", { text: i.title }),
      el("td", { className: "num", text: pounds(i.list_price_pence) }),
      el("td", { className: "num", text: pounds(i.floor_price_pence) }),
      el("td", { className: "num", text: String(i.stock) }),
    ])));

    const itemIds = new Set(items.map((i) => i.id));
    const sales = data.orders.filter((o) => itemIds.has(o.inventory_id) && o.status === "paid");
    $("store-sales-empty").hidden = sales.length > 0;
    $("store-sales").replaceChildren(...sales.map((o) => {
      const item = items.find((i) => i.id === o.inventory_id);
      const draft = data.events.find((e) => e.request_id === o.request_id && e.type === "shopify_draft_order");
      const total = o.price_pence * o.quantity;
      const fee = Math.round(total * (boot.take_rate_bps || 0) / 10000);
      return el("li", { className: "sale" }, [
        el("strong", { text: (o.quantity > 1 ? o.quantity + " \u00D7 " : "") + (item ? item.title : o.inventory_id) + " sold for " + pounds(total) }),
        el("span", { text: "Exchange fee " + pounds(fee) + " (" + (boot.take_rate_bps / 100) + "%). You receive " + pounds(total - fee) + "." }),
        el("span", { text: draft ? "Shopify draft order " + (draft.payload.draft_order_name || "") + " created" : "Recorded in the exchange ledger. Payment simulated." }),
      ]);
    }));

    const mine = data.quotes.filter((q) => q.merchant_id === storeMerchant && q.origin !== "reprice");
    const won = sales.length;
    $("store-quotes").replaceChildren(
      el("div", {}, [el("dt", { text: "Quotes your agent sent" }), el("dd", { text: String(mine.length) })]),
      el("div", {}, [el("dt", { text: "Rejected by the exchange" }), el("dd", { text: String(mine.filter((q) => q.status === "rejected").length) })]),
      el("div", {}, [el("dt", { text: "Sales won" }), el("dd", { text: String(won) })]),
    );
  }

  $("store-merchant").addEventListener("change", (event) => {
    storeMerchant = event.currentTarget.value;
    if (snapshot) renderStore(snapshot);
  });

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

  // ------------------------------------------------------------------ actions

  async function sendRequest(text, statusId, button) {
    button.disabled = true;
    setStatus(statusId, "Your agent is asking the shops and negotiating…");
    try {
      const outcome = await api("/requests", { text });
      selectedId = outcome.request.id;
      setStatus(statusId, "Request " + (STATUS_TEXT[outcome.request.status] || outcome.request.status).toLowerCase() + ".");
    } catch (error) {
      if (error.requestId) selectedId = error.requestId;
      setStatus(statusId, (error.code ? "Blocked by your rules: " : "") + error.message, true);
    } finally {
      button.disabled = false;
      refresh();
    }
  }

  $("request-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendRequest($("request-text").value, "request-status",
                event.submitter || event.target.querySelector("button[type=submit]"));
  });

  $("shop-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendRequest($("shop-text").value, "shop-status",
                event.submitter || event.target.querySelector("button[type=submit]"));
  });

  for (const preset of document.querySelectorAll(".preset")) {
    preset.addEventListener("click", () => {
      const target = $(preset.dataset.target || "request-text");
      target.value = preset.dataset.text;
      target.focus();
    });
  }

  async function reprice(merchantId, inventoryId, pricePence, statusId) {
    setStatus(statusId, "Repricing and rechecking resting orders…");
    try {
      const result = await api("/merchants/" + merchantId + "/reprice", { inventory_id: inventoryId, price_pence: pricePence });
      const fills = result.filled_order_ids.length;
      if (fills && snapshot) {
        const resting = snapshot.requests.find((r) => r.status === "resting");
        if (resting) selectedId = resting.id;
      }
      setStatus(statusId, fills ? "Filled " + fills + " resting order" + (fills > 1 ? "s" : "") + "." : "Price updated. No resting order fits yet.");
    } catch (error) {
      setStatus(statusId, error.message, true);
    }
    refresh();
  }

  $("flash-sale").addEventListener("click", async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const sale = boot.flash_sale || { merchant_id: "merchant-bassline", inventory_id: "inventory-bassline-pro", price_pence: 14800 };
      await reprice(sale.merchant_id, sale.inventory_id, sale.price_pence, "reprice-status");
    } finally {
      button.disabled = false;
    }
  });

  $("reprice-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const option = form.inventory.selectedOptions[0];
    if (!option) return;
    await reprice(option.dataset.merchant, option.value, toPence(form.price.value), "reprice-status");
  });

  $("reprice-item").addEventListener("change", () => {
    const item = snapshot && snapshot.inventory.find((i) => i.id === $("reprice-item").value);
    if (item) $("reprice-form").price.value = (item.floor_price_pence / 100).toFixed(2);
  });

  async function setKilled(turningOn) {
    if (turningOn && !window.confirm("Turn the kill switch on? The risk gate will block every order until you turn it off.")) {
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

  $("kill").addEventListener("change", async (event) => {
    const box = event.currentTarget;
    const turningOn = box.checked;
    const ok = await setKilled(turningOn);
    if (!ok) box.checked = !turningOn;
  });

  $("shop-pause").addEventListener("click", () => {
    const killed = Boolean(snapshot && snapshot.mandate && snapshot.mandate.killed);
    setKilled(!killed);
  });

  $("mandate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const allowed = [...form.querySelectorAll("input[name=merchant]:checked")].map((i) => i.value);
    if (!allowed.length) {
      setStatus("mandate-status", "Choose at least one merchant.", true);
      return;
    }
    try {
      await api("/mandate", {
        budget_pence: toPence(form.budget.value),
        max_per_order_pence: toPence(form.cap.value),
        allowed_merchant_ids: allowed,
        orders_per_minute: Number(form.velocity.value),
      });
      setStatus("mandate-status", "Mandate set. Spend starts at £0.00.");
    } catch (error) {
      setStatus("mandate-status", error.message, true);
    }
    refresh();
  });

  start();
})();
