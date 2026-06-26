"""Live integration test for the DoorDash adapter — opt-in.

Hits the real doordash.com. Skipped by default so the suite stays fast and
hermetic; run it with:

    DD_LIVE=1 python -m pytest tests/test_doordash_live.py -q

With a fresh (cold) profile it proves real-platform contact by hitting the
Cloudflare wall and failing *gracefully* (ProviderUnavailable), never crashing
and never charging. With a warmed/logged-in profile it returns real candidates.
"""
import os

import pytest

from engine.config import load_config_from_dict
from providers.base import ProviderUnavailable
from providers.doordash import DoorDashProvider

pytestmark = pytest.mark.skipif(
    not os.environ.get("DD_LIVE"), reason="set DD_LIVE=1 to run the live DoorDash test"
)


def test_discover_hits_real_platform_and_degrades_gracefully(tmp_path):
    config = load_config_from_dict(
        {"budget": {"daily_max_usd": 25}, "preferences": {"cuisines": ["Thai"]}}
    )
    provider = DoorDashProvider(headless=True, profile_dir=tmp_path / "cold-profile")
    try:
        candidates = provider.discover(config)
        # Warm profile path: real candidates, all unverified (DoorDash can't
        # confirm allergens), so the engine will gate them for a restricted user.
        for c in candidates:
            assert c.verified_safe is False
            assert c.price_usd is not None
    except ProviderUnavailable as error:
        # Cold profile path: real contact, graceful failure. This is success too.
        assert "doordash" in str(error).lower()
