from run import StepStatus, run


def test_run_walking_skeleton_reaches_decision():
    result = run("user_preferences.yaml")

    assert [step.number for step in result.steps] == list(range(1, 12))
    assert result.decision is not None
    assert result.steps[-1].status is StepStatus.OK
