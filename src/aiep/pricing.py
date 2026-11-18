"""Token usage and cost, computed from measured tokens and the recorded price table.

Costs are *computed* (measured token counts x ``config/model_pricing.json``), never
fabricated or taken from a client. Money uses ``Decimal``. Rates are per million tokens.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

MTOK = Decimal(1_000_000)
_RATE_KEYS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")


class PricingError(ValueError):
    pass


@dataclass(frozen=True)
class Pricing:
    model: str
    input: Decimal
    output: Decimal
    cache_read: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    retrieved: str
    sources: tuple[str, ...]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    cache_write_unclassified_tokens: int = 0  # priced at the 1h rate (upper bound)

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Cost:
    usd: Decimal
    upper_bound: bool  # True if any cache-write tokens were unclassified


def load_pricing(path: Path, model: str) -> Pricing:
    data = json.loads(Path(path).read_text())
    for key in ("retrieved", "sources"):
        if not data.get(key):
            raise PricingError(f"pricing file is missing {key!r}")
    entry = (data.get("models") or {}).get(model)
    if not entry:
        raise PricingError(f"no pricing for model {model!r}")
    rates = {}
    for key in _RATE_KEYS:
        if key not in entry:
            raise PricingError(f"pricing for {model!r} is missing {key!r}")
        try:
            rate = Decimal(str(entry[key]))
        except Exception as e:
            raise PricingError(f"pricing {key!r} is not a number") from e
        if rate < 0:
            raise PricingError(f"pricing {key!r} is negative")
        rates[key] = rate
    return Pricing(model=model, retrieved=data["retrieved"], sources=tuple(data["sources"]), **rates)


def _nonneg_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PricingError(f"usage value {value!r} is not a non-negative integer")
    return value


def usage_from_api(usage: Mapping[str, Any]) -> Usage:
    """Normalise a Messages API ``usage`` object.

    Field names per the pricing docs: ``input_tokens``, ``output_tokens``,
    ``cache_read_input_tokens``, ``cache_creation_input_tokens``. A ``cache_creation``
    breakdown (5m/1h) is honoured only if present and consistent with the total;
    otherwise all cache-creation tokens count as unclassified (priced at 1h).
    """
    u = Usage(
        input_tokens=_nonneg_int(usage.get("input_tokens") or 0),
        output_tokens=_nonneg_int(usage.get("output_tokens") or 0),
        cache_read_tokens=_nonneg_int(usage.get("cache_read_input_tokens") or 0),
    )
    total = _nonneg_int(usage.get("cache_creation_input_tokens") or 0)
    breakdown = usage.get("cache_creation")
    if isinstance(breakdown, Mapping):
        five = _nonneg_int(breakdown.get("ephemeral_5m_input_tokens") or 0)
        hour = _nonneg_int(breakdown.get("ephemeral_1h_input_tokens") or 0)
        if five + hour == total:
            u.cache_write_5m_tokens, u.cache_write_1h_tokens = five, hour
            return u
    u.cache_write_unclassified_tokens = total
    return u


def compute_cost(p: Pricing, u: Usage) -> Cost:
    usd = (
        Decimal(u.input_tokens) * p.input
        + Decimal(u.output_tokens) * p.output
        + Decimal(u.cache_read_tokens) * p.cache_read
        + Decimal(u.cache_write_5m_tokens) * p.cache_write_5m
        + Decimal(u.cache_write_1h_tokens + u.cache_write_unclassified_tokens) * p.cache_write_1h
    ) / MTOK
    return Cost(usd=usd, upper_bound=u.cache_write_unclassified_tokens > 0)
