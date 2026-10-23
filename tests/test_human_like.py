"""Human-likeness mechanisms: sustained attention, interest and revive candidates."""
from datetime import datetime, timedelta
import sqlite3

import pytest

from myatry.attention import followup
from myatry.proactive import (
    DEFAULTS,
    Candidate,
    ProactiveScheduler,
    ProactiveSettings,
    SELF_INITIATED_METADATA,
)


@pytest.fixture
def db():
    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE attention_windows(scope TEXT PRIMARY KEY,"
                        "remaining INTEGER,expires REAL,wake_key TEXT)")
    return connection


def test_attention_grants_two_to_four_rounds(db):
    rounds = set()
    for number in range(60):
        result = followup(db, "s", f"key{number}", wake=True, now=1_000.0)
        rounds.add(result["remaining"])
    assert rounds == {2, 3, 4}


def test_attention_window_expires(db):
    followup(db, "s", "k", wake=True, now=1_000.0, ttl=100)
    assert followup(db, "s", "k2", wake=False, now=1_050.0)["active"] is True
    assert followup(db, "s", "k3", wake=False, now=1_200.0)["active"] is False


def test_settings_accept_new_keys_and_reject_unknown():
    settings = ProactiveSettings.from_config({
        "tick_interval_minutes": 10,
        "revive_silence_hours": 3,
        "interest_keywords": ["烤肉", "命令行"],
    })
    assert settings.tick_interval_minutes == 10.0
    assert settings.revive_silence_hours == 3.0
    assert settings.interest_keywords == ("烤肉", "命令行")
    with pytest.raises(ValueError):
        ProactiveSettings.from_config({"no_such_key": 1})
    with pytest.raises(ValueError):
        ProactiveSettings.from_config({"interest_keywords": "烤肉"})


def scheduler(*, state, candidates, config=None, deliver_result=None):
    delivered = []

    async def generate(*, scope, candidate, now, instruction):
        return {"text": "冒个泡", "metadata": dict(SELF_INITIATED_METADATA)}

    async def deliver(*, scope, candidate, draft, now):
        delivered.append(candidate.kind)
        return deliver_result or {"id": "d1", "status": "prepared"}

    return ProactiveScheduler(
        config or {"enabled": True, "shadow_mode": False, "active_start": "00:00",
                   "active_end": "23:59", "tick_interval_minutes": 10,
                   "revive_silence_hours": 3},
        scope="a" * 32,
        state=lambda scope: state,
        candidates=lambda scope: candidates,
        generate=generate,
        deliver=deliver,
        clock=lambda: datetime.now().astimezone(),
        diagnostic=lambda record: None,
    ), delivered


def base_state(**updates):
    now = datetime.now().astimezone()
    state = {
        "last_speech_at": None,
        "proactive_today": 0,
        "group_last_message_at": now - timedelta(hours=5),
        "budget_used_ratio": 0.1,
        "explicit_reply_pending": False,
        "revive_available": False,
    }
    state.update(updates)
    return state


@pytest.mark.asyncio
async def test_revive_flag_lets_silent_group_speak():
    candidate = Candidate(candidate_id="revive:today", kind="group_revive",
                          created_at=datetime.now().astimezone())
    rig, delivered = scheduler(
        state=base_state(revive_available=True), candidates=[candidate])
    result = await rig.tick()
    assert result["skipped"] is None and "revive" in result["gates"]
    assert delivered == ["group_revive"]


@pytest.mark.asyncio
async def test_silent_group_without_revive_still_skips():
    candidate = Candidate(candidate_id="q1", kind="unanswered_question",
                          created_at=datetime.now().astimezone())
    rig, delivered = scheduler(
        state=base_state(revive_available=False), candidates=[candidate])
    result = await rig.tick()
    assert result["skipped"] == "group_silent" and not delivered


@pytest.mark.asyncio
async def test_interest_candidate_can_speak_in_active_group():
    now = datetime.now().astimezone()
    candidate = Candidate(candidate_id="m9", kind="interesting_exchange",
                          created_at=now, excerpt="烤肉走起？")
    rig, delivered = scheduler(
        state=base_state(group_last_message_at=now - timedelta(minutes=2)),
        candidates=[candidate])
    result = await rig.tick()
    assert result["skipped"] is None and delivered == ["interesting_exchange"]


def test_defaults_keep_conservative_off():
    assert DEFAULTS["enabled"] is False and DEFAULTS["shadow_mode"] is True
    assert DEFAULTS["tick_interval_minutes"] == 0.0
    assert DEFAULTS["revive_silence_hours"] == 0.0
