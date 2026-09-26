import pytest
from pydantic import ValidationError

from app.config import ConfigurationError, Settings
from app.domain.models import CheckoutCommand, MandateCreate, QuoteCreate, RFQCreate


def test_money_and_quantities_are_positive_integers():
    with pytest.raises(ValidationError):
        RFQCreate(query="headphones", category="audio", max_price_pence=150.5,
                  quantity=1, deadline="2026-09-27T17:00:00Z")
    with pytest.raises(ValidationError):
        RFQCreate(query="headphones", category="audio", max_price_pence=15000,
                  quantity=0, deadline="2026-09-27T17:00:00Z")


def test_spend_limit_cannot_exceed_budget():
    with pytest.raises(ValidationError):
        MandateCreate(budget_pence=30_000, max_per_order_pence=90_000,
                      allowed_merchant_ids=["merchant-1"], orders_per_minute=3)


def test_inputs_reject_client_controlled_fields():
    with pytest.raises(ValidationError):
        QuoteCreate(request_id="r", merchant_id="m", inventory_id="i", price_pence=100,
                    delivery_days=1, status="accepted")
    with pytest.raises(ValidationError):
        CheckoutCommand(request_id="r", quote_id="q", idempotency_key="k", order_id="o")
    with pytest.raises(ValidationError):
        MandateCreate(budget_pence=30_000, max_per_order_pence=20_000, spent_pence=0,
                      allowed_merchant_ids=["m"], orders_per_minute=3)


def test_offline_defaults_need_no_secrets():
    settings = Settings(_env_file=None)
    assert settings.profile == "offline"
    assert settings.llm_mode == "fake"
    assert settings.merchant_timeout_seconds == 8
    assert settings.negotiation_timeout_seconds == 30
    assert settings.max_rounds == 3


def test_connected_profile_fails_loudly_without_credentials():
    with pytest.raises(ConfigurationError) as error:
        Settings(_env_file=None, profile="connected")
    message = str(error.value)
    assert "SUPABASE_URL" in message and "GROK_API_KEY" in message
