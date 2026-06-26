"""The run pipeline drives whatever provider it is given, and routes every
failure scenario to BLOCK + places nothing. These are the brief's triggerable
failure paths (budget exceeded, item unavailable), exercised end to end.
"""
from engine.models import DecisionStatus
from providers.base import OrderStatus
from providers.mock import MockProvider
from run import run


def _write_config(tmp_path, **budget):
    b = {"daily_max_usd": 25, "auto_approve_under_usd": 18}
    b.update(budget)
    lines = ["budget:"] + [f"  {k}: {v}" for k, v in b.items()]
    lines += ["restrictions:", "  dietary: [vegetarian]"]
    path = tmp_path / "preferences.yaml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_run_uses_injected_provider_and_places_on_auto(tmp_path):
    config = _write_config(tmp_path)
    result = run(config, provider=MockProvider("happy"))
    assert result.decision.status is DecisionStatus.AUTO
    assert result.placed is True
    assert result.order_result is not None
    assert result.order_result.status is OrderStatus.PLACED
    assert result.order_result.charged is False


def test_run_over_budget_blocks_and_places_nothing(tmp_path):
    config = _write_config(tmp_path)
    result = run(config, provider=MockProvider("over_budget"))
    assert result.decision.status is DecisionStatus.BLOCK
    assert result.decision.reason == "over_daily_max"
    assert result.placed is False
    assert result.order_result is None  # nothing carted on a hard-line block


def test_run_unavailable_blocks_and_places_nothing(tmp_path):
    config = _write_config(tmp_path)
    result = run(config, provider=MockProvider("unavailable"))
    assert result.decision.status is DecisionStatus.BLOCK
    assert result.placed is False


def test_run_empty_discovery_blocks_and_places_nothing(tmp_path):
    config = _write_config(tmp_path)
    result = run(config, provider=MockProvider("empty"))
    assert result.decision.status is DecisionStatus.BLOCK
    assert result.decision.reason == "no_candidate"
    assert result.placed is False


def test_run_result_to_dict_includes_order_result(tmp_path):
    config = _write_config(tmp_path)
    result = run(config, provider=MockProvider("happy"))
    payload = result.to_dict()
    assert "order_result" in payload
    assert payload["order_result"]["charged"] is False
    assert payload["placed"] is True
