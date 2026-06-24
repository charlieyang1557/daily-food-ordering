import pytest

from engine.config import ConfigError, load_config_from_dict


def test_load_config_validates_required_budget_and_defaults():
    config = load_config_from_dict(
        {
            "budget": {
                "daily_max_usd": 25,
                "auto_approve_under_usd": 18,
            },
            "restrictions": {
                "dietary": ["vegetarian"],
                "allergies": ["peanuts"],
                "never_order": ["KFC"],
            },
        }
    )

    assert config.schedule.order_time == "12:00"
    assert config.budget.daily_max_usd == 25
    assert config.budget.auto_approve_under_usd == 18
    assert config.restrictions.allergies == ["peanuts"]

    with pytest.raises(ConfigError):
        load_config_from_dict({"budget": {"auto_approve_under_usd": 10}})


@pytest.mark.parametrize(
    "bad_config",
    [
        {
            "schedule": {"timezone": "America/EST"},
            "budget": {"daily_max_usd": 25},
        },
        {
            "budget": {"daily_max_usd": 25},
            "restrictions": {"allergies": ["tuna"]},
        },
        {
            "budget": {"daily_max_usd": 25},
            "restrictions": {"never_order": ["cilantro"]},
        },
    ],
)
def test_load_config_rejects_emily_demo_flaws(bad_config):
    with pytest.raises(ConfigError):
        load_config_from_dict(bad_config)
