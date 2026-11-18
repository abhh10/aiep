import json
from decimal import Decimal
from pathlib import Path

import pytest

from aiep.pricing import PricingError, Usage, compute_cost, load_pricing, usage_from_api

REAL = Path(__file__).resolve().parents[1]
PRICING = REAL / "config" / "model_pricing.json"
MODEL = "claude-sonnet-5-5"


def test_experiment_pins_one_model_that_has_pricing():
    exp = json.loads((REAL / "config" / "experiment.json").read_text())
    assert exp["model"] == MODEL
    p = load_pricing(PRICING, exp["model"])
    assert (p.input, p.output, p.cache_read, p.cache_write_5m, p.cache_write_1h) == (
        Decimal("2"), Decimal("10"), Decimal("0.10"), Decimal("2.50"), Decimal("4"))
    assert p.retrieved == "2026-11-19"
    assert all(s.startswith("https://platform.claude.com/") for s in p.sources)


def test_cost_is_computed_per_million_tokens_with_decimal():
    p = load_pricing(PRICING, MODEL)
    u = Usage(input_tokens=1000, output_tokens=200, cache_read_tokens=5000,
              cache_write_5m_tokens=100, cache_write_1h_tokens=300)
    c = compute_cost(p, u)
    assert c.usd == Decimal("0.00595") and c.upper_bound is False


def test_unclassified_cache_writes_priced_at_1h_and_flagged():
    p = load_pricing(PRICING, MODEL)
    c = compute_cost(p, Usage(cache_write_unclassified_tokens=1_000_000))
    assert c.usd == Decimal("4") and c.upper_bound is True


def test_usage_from_api_documented_field_names():
    u = usage_from_api({"input_tokens": 105, "output_tokens": 6039,
                        "cache_read_input_tokens": 7123, "cache_creation_input_tokens": 7345})
    assert (u.input_tokens, u.output_tokens, u.cache_read_tokens, u.cache_write_unclassified_tokens) == (105, 6039, 7123, 7345)


def test_breakdown_used_only_when_consistent_with_total():
    ok = usage_from_api({"cache_creation_input_tokens": 400,
                         "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 300}})
    assert (ok.cache_write_5m_tokens, ok.cache_write_1h_tokens, ok.cache_write_unclassified_tokens) == (100, 300, 0)
    bad = usage_from_api({"cache_creation_input_tokens": 400,
                          "cache_creation": {"ephemeral_5m_input_tokens": 1, "ephemeral_1h_input_tokens": 1}})
    assert bad.cache_write_unclassified_tokens == 400 and bad.cache_write_5m_tokens == 0


@pytest.mark.parametrize("usage", [{"input_tokens": -1}, {"output_tokens": "5"}, {"input_tokens": True}, {"input_tokens": 1.5}])
def test_malformed_usage_rejected(usage):
    with pytest.raises(PricingError):
        usage_from_api(usage)


def test_missing_fields_default_to_zero():
    assert usage_from_api({}).as_dict() == Usage().as_dict()


@pytest.mark.parametrize("mutate", [
    lambda d: d["models"]["claude-sonnet-5-5"].pop("cache_read"),
    lambda d: d["models"]["claude-sonnet-5-5"].__setitem__("output", "abc"),
    lambda d: d["models"]["claude-sonnet-5-5"].__setitem__("input", "-1"),
    lambda d: d.pop("retrieved"),
    lambda d: d.__setitem__("sources", []),
    lambda d: d["models"].clear(),
])
def test_pricing_file_must_be_complete(tmp_path, mutate):
    d = json.loads(PRICING.read_text())
    mutate(d)
    f = tmp_path / "p.json"
    f.write_text(json.dumps(d))
    with pytest.raises(PricingError):
        load_pricing(f, MODEL)
