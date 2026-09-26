import re
from pathlib import Path

from bs4 import BeautifulSoup

STATIC = Path(__file__).resolve().parents[2] / "app" / "static"
HTML = (STATIC / "index.html").read_text()
JS = (STATIC / "app.js").read_text()
CSS = (STATIC / "styles.css").read_text()


def test_dashboard_has_three_named_regions():
    soup = BeautifulSoup(HTML, "html.parser")
    assert soup.select_one('[aria-label="Mandate controls"]')
    assert soup.select_one('[aria-label="Request and quote market"]')
    assert soup.select_one('[aria-label="Risk and event feed"]')


def test_dashboard_supports_offline_and_realtime_sources():
    assert 'fetch("/dashboard")' in JS
    assert ".channel(" in JS
    assert "setInterval" in JS


def test_supabase_script_is_pinned_and_loaded_only_for_realtime():
    assert re.search(r"supabase-js@2\.\d+\.\d+/", JS)
    assert "supabase" not in HTML.lower()  # never loaded by the offline page itself
    start = JS.index("async function start()")
    realtime_branch = JS.index("boot.realtime", start)
    assert JS.index("startRealtime(boot)", start) > realtime_branch


def test_realtime_is_read_only_and_mutations_go_to_fastapi():
    assert ".insert(" not in JS and ".update(" not in JS and ".rpc(" not in JS
    assert '"/merchants/" + merchantId + "/reprice"' in JS
    for path in ("/requests", "/kill", "/mandate"):
        assert f'"{path}"' in JS


def test_payload_text_is_never_injected_as_html():
    assert "innerHTML" not in JS
    assert "textContent" in JS


def test_approved_tokens_layout_and_accessibility():
    # Grok-style dark theme (overrides the spec's ledger palette at the user's request).
    for token in ("--bg: #000000", "--text: #F4F4F5", "--fill:", "--block:", "color-scheme: dark"):
        assert token in CSS
    assert "minmax(15rem, .8fr) minmax(28rem, 2fr) minmax(18rem, 1fr)" in CSS
    assert "@media (max-width: 900px)" in CSS
    assert "prefers-reduced-motion" in CSS
    assert ":focus-visible" in CSS
    assert "2.75rem" in CSS  # 44px minimum action targets


def test_kill_switch_is_native_and_payment_is_labelled_simulated():
    soup = BeautifulSoup(HTML, "html.parser")
    kill = soup.select_one("#kill")
    assert kill.name == "input" and kill["type"] == "checkbox"
    assert soup.select_one('label[for="kill"]')
    assert "Simulated payment" in JS
    assert "simulated" in HTML.lower()
    assert "window.confirm" in JS and "turningOn &&" in JS  # confirm only when turning on


def test_flash_sale_comes_from_bootstrap_config():
    assert "reprice(sale.merchant_id, sale.inventory_id, sale.price_pence" in JS
    assert "inventory_id: inventoryId, price_pence: pricePence" in JS


def test_every_architecture_layer_is_named_in_the_ui():
    for layer in ("Grok agents", "Matching", "Risk gate", "Ledger", "Merchant store"):
        assert layer in HTML
        assert layer in JS


def test_risk_gate_checks_mirror_policy_order():
    from app.domain.policy import POLICY_REASONS  # noqa: F401 - codes exist
    order = ["KILL_SWITCH_ON", "MERCHANT_NOT_ALLOWED", "OUT_OF_STOCK", "BELOW_FLOOR",
             "ABOVE_REQUEST_MAX", "PER_ORDER_CAP_EXCEEDED", "VELOCITY_LIMIT_EXCEEDED",
             "BUDGET_EXCEEDED", "REFERENCE_PRICE_EXCEEDED"]
    positions = [JS.index(f'["{code}"') for code in order]
    assert positions == sorted(positions)


def test_disconnected_banner_retains_snapshot():
    assert "Disconnected from the exchange API" in JS
    assert "Showing the last snapshot" in JS
