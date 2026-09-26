(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const money = p => new Intl.NumberFormat("en-GB", {style:"currency", currency:"GBP"}).format((p || 0) / 100);
  const node = (tag, text, className) => { const n = document.createElement(tag); n.textContent = text; if (className) n.className = className; return n; };
  const labels = {paid:"Filled",resting:"Waiting for a better price",rejected:"Blocked by rules",out_of_stock:"Stock unavailable"};
  function metric(label, value) { const n = node("div", ""); n.append(node("strong", value), node("span", label)); return n; }
  $("arena-run").addEventListener("click", async () => {
    const button = $("arena-run"); button.disabled = true;
    $("arena-status").textContent = "Buyer requests → seller quotes → negotiation → guarded execution…";
    try {
      const response = await fetch("/simulate/arena", {method:"POST",headers:{"Content-Type":"application/json"},body:"{}"});
      if (!response.ok) throw new Error("The arena could not finish. Please retry. HTTP " + response.status);
      const d = await response.json();
      $("arena-empty").hidden = true; $("arena-results").hidden = false;
      const mode = {live_grok:"Live Grok proposals",replay:"Recorded agent replay",deterministic:"Deterministic agent simulation"}[d.mode] || d.mode;
      $("arena-status").textContent = mode + " · " + d.seconds.toFixed(2) + " seconds · " + d.filled + " fills, " + d.blocked + " blocked";
      $("arena-summary").replaceChildren(metric("Trade volume", money(d.gmvolume_pence)),metric("Buyer savings vs quotes",money(d.savings_pence)),metric("Projected exchange fees",money(d.fees_pence)),metric("Budget remaining",money(d.remaining_pence)));
      $("arena-buyers").replaceChildren(...d.buyers.map(b => {
        const n = node("article", "", "arena-agent " + b.status);
        n.append(node("h4", b.agent.replace(/^Buyer agent: /,"")),node("p",b.strategy),node("strong",labels[b.status] || b.status),node("p",b.units + " units · " + money(b.spent_pence) + " spent"));
        if (b.code) n.append(node("small",b.code.replaceAll("_"," ")));
        return n;
      }));
      $("arena-merchants").replaceChildren(...d.merchants.map(m => {
        const n = node("article", "", "arena-agent");
        n.append(node("h4",m.name),node("p",m.units_sold + " units sold"),node("strong",money(m.net_pence) + " after proposed fee"),node("p",money(m.gross_pence) + " gross − " + money(m.fee_pence) + " fee")); return n;
      }));
      $("arena-proof").replaceChildren(...Object.entries(d.invariants).map(([key,ok]) => node("span",(ok ? "✓ " : "✗ ") + key.replaceAll("_"," "),ok ? "proof-pass" : "proof-fail")));
      $("arena-timeline").replaceChildren(...d.timeline.map(e => node("li",e.type.replaceAll("_"," ") + " — " + JSON.stringify(e.payload))));
      button.textContent = "Run a fresh market";
    } catch (e) { $("arena-status").textContent = e.message; }
    finally { button.disabled = false; }
  });
})();
