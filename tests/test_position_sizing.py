"""
Tests for position_sizing.decide_tier_step() - the pure tier-stepping
rule behind Position Management (see that module's own docstring). No
database or network involved: these just feed hand-built win/loss
counts and window keys straight into the function and check the
resulting state.

TIER_PCTS below mirrors the app's own default (Tier 1 = index 0 = full
size, Tier 4 = index 3 = smallest).
"""
from position_sizing import decide_tier_step

TIER_PCTS = [100, 75, 50, 25]


def _state(tier_index=0, down_key=None, up_key=None):
    return {
        "current_tier_index": tier_index,
        "last_down_window_key": down_key,
        "last_up_window_key": up_key,
    }


def test_no_change_when_neither_threshold_met():
    state = _state(tier_index=1)
    result = decide_tier_step(state, TIER_PCTS, losers=1, winners=1, loss_threshold=3, win_threshold=3, window_key="A")
    assert result["current_tier_index"] == 1
    assert result["step_direction"] is None


def test_steps_down_one_tier_on_loss_trigger():
    state = _state(tier_index=0)
    result = decide_tier_step(state, TIER_PCTS, losers=3, winners=2, loss_threshold=3, win_threshold=4, window_key="A")
    assert result["current_tier_index"] == 1
    assert result["step_direction"] == "down"
    assert result["last_down_window_key"] == "A"


def test_does_not_double_step_on_the_same_window():
    # Same window key as the one that already caused a step down -
    # simulates re-loading the page with nothing new having happened.
    state = _state(tier_index=1, down_key="A")
    result = decide_tier_step(state, TIER_PCTS, losers=3, winners=2, loss_threshold=3, win_threshold=4, window_key="A")
    assert result["current_tier_index"] == 1
    assert result["step_direction"] is None


def test_steps_down_again_on_a_genuinely_new_window():
    # A different window key (e.g. a new trade closed, or an open
    # position's live P/L flipped) with the loss condition still met -
    # this SHOULD trigger a further step down.
    state = _state(tier_index=1, down_key="A")
    result = decide_tier_step(state, TIER_PCTS, losers=3, winners=2, loss_threshold=3, win_threshold=4, window_key="B")
    assert result["current_tier_index"] == 2
    assert result["step_direction"] == "down"
    assert result["last_down_window_key"] == "B"


def test_floor_at_lowest_tier():
    lowest_index = len(TIER_PCTS) - 1
    state = _state(tier_index=lowest_index, down_key="A")
    result = decide_tier_step(state, TIER_PCTS, losers=5, winners=0, loss_threshold=3, win_threshold=4, window_key="B")
    assert result["current_tier_index"] == lowest_index
    assert result["step_direction"] is None


def test_steps_up_one_tier_on_win_trigger():
    state = _state(tier_index=2)
    result = decide_tier_step(state, TIER_PCTS, losers=1, winners=4, loss_threshold=3, win_threshold=4, window_key="A")
    assert result["current_tier_index"] == 1
    assert result["step_direction"] == "up"
    assert result["last_up_window_key"] == "A"


def test_does_not_double_step_up_on_the_same_window():
    state = _state(tier_index=1, up_key="A")
    result = decide_tier_step(state, TIER_PCTS, losers=1, winners=4, loss_threshold=3, win_threshold=4, window_key="A")
    assert result["current_tier_index"] == 1
    assert result["step_direction"] is None


def test_ceiling_at_full_size():
    state = _state(tier_index=0, up_key="A")
    result = decide_tier_step(state, TIER_PCTS, losers=0, winners=5, loss_threshold=3, win_threshold=4, window_key="B")
    assert result["current_tier_index"] == 0
    assert result["step_direction"] is None


def test_down_takes_priority_when_both_thresholds_are_met():
    # Documents the tie-break: if a window somehow satisfies both the
    # loss and win trigger at once, stepping down wins.
    state = _state(tier_index=1)
    result = decide_tier_step(state, TIER_PCTS, losers=3, winners=3, loss_threshold=3, win_threshold=3, window_key="A")
    assert result["current_tier_index"] == 2
    assert result["step_direction"] == "down"
