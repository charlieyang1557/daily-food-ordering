from engine.models import DecisionStatus
from run import StepStatus, run


def test_run_walking_skeleton_reaches_decision(tmp_path):
    config_path = tmp_path / "preferences.yaml"
    config_path.write_text(
        """
schedule:
  order_time: "12:30"
  timezone: "America/Los_Angeles"
budget:
  daily_max_usd: 25
  auto_approve_under_usd: 18
preferences:
  cuisines: [Thai]
  favorite_restaurants: ["Hermetic Cafe"]
restrictions:
  dietary: [vegetarian]
""".lstrip(),
        encoding="utf-8",
    )

    result = run(config_path)

    assert [step.number for step in result.steps] == list(range(1, 12))
    assert result.decision is not None
    assert result.selected_candidate is not None
    assert result.selected_candidate.restaurant == "Thai Spice"
    assert result.selected_candidate.item_name == "Vegetarian Pad Thai"
    assert result.selected_candidate.cuisine == "Thai"
    assert result.decision.status is DecisionStatus.AUTO
    assert result.decision.reason == "within_auto_approve"
    assert result.placed is True
    assert result.steps[-1].status is StepStatus.OK
