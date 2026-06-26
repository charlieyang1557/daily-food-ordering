"""CLI + pipeline-resolution coverage: exit codes for bad config and an
unavailable provider, and the CONFIRM band placing nothing.
"""
import json

from engine.models import DecisionStatus
from providers.base import ProviderUnavailable
from providers.mock import MockProvider
import run as run_module
from run import StepStatus, run


def _good_config(tmp_path):
    path = tmp_path / "good.yaml"
    path.write_text(
        "budget:\n  daily_max_usd: 25\n  auto_approve_under_usd: 18\n", encoding="utf-8"
    )
    return path


def test_cli_config_invalid_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.yaml"
    # auto_approve_under > daily_max is a contradiction the loader rejects.
    bad.write_text("budget:\n  daily_max_usd: 10\n  auto_approve_under_usd: 99\n", encoding="utf-8")
    code = run_module.main(["--config", str(bad), "--provider", "mock"])
    assert code == 2
    assert json.loads(capsys.readouterr().out)["error"] == "config_invalid"


def test_cli_provider_unavailable_exits_3(tmp_path, capsys, monkeypatch):
    class _DeadProvider:
        name = "dead"

        def discover(self, config):
            raise ProviderUnavailable("doordash bot wall (Cloudflare)")

        def place_order(self, *a, **k):  # pragma: no cover - never reached
            raise AssertionError

    monkeypatch.setattr(run_module, "_build_provider", lambda args: _DeadProvider())
    code = run_module.main(["--config", str(_good_config(tmp_path)), "--provider", "doordash"])
    assert code == 3
    assert json.loads(capsys.readouterr().out)["error"] == "provider_unavailable"


def test_confirm_band_resolves_to_skipped_and_places_nothing(tmp_path):
    # $14 happy candidate, but auto_approve lowered to 10 -> 10 < 14 <= 25 -> CONFIRM.
    cfg = tmp_path / "confirm.yaml"
    cfg.write_text(
        "budget:\n  daily_max_usd: 25\n  auto_approve_under_usd: 10\n", encoding="utf-8"
    )
    result = run(cfg, provider=MockProvider("happy"))
    assert result.decision.status is DecisionStatus.CONFIRM
    assert result.decision.reason == "above_auto_approve"
    assert result.placed is False
    assert result.order_result is None
    # step 8 (resolve_decision) reports SKIPPED for a CONFIRM that isn't yet approved
    assert result.steps[7].status is StepStatus.SKIPPED
