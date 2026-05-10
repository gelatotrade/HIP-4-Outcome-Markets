"""Feed integration test against the simulator."""

from __future__ import annotations

from src.feed import Feed
from src.simulator import fast_forward


def test_simulator_snapshot_has_legs():
    feed = Feed(allow_live=False, history_len=20, threshold_vol=0.04)
    for _ in range(3):
        fast_forward(60.0)
        feed.snapshot()
    last = feed.history[-1]
    assert last.source == "simulated"
    assert last.spot > 0
    assert last.binaries
    assert last.statarb is not None


def test_history_capped():
    feed = Feed(allow_live=False, history_len=5, threshold_vol=0.04)
    for _ in range(15):
        fast_forward(60.0)
        feed.snapshot()
    assert len(feed.history) == 5


def test_set_params_propagates():
    feed = Feed(allow_live=False, history_len=5, threshold_vol=0.50)
    feed.snapshot()
    n_before = feed.history[-1].statarb.n_active
    feed.set_params(threshold_vol=0.001)
    feed.snapshot()
    n_after = feed.history[-1].statarb.n_active
    # Lower threshold ⇒ at least as many active legs
    assert n_after >= n_before
