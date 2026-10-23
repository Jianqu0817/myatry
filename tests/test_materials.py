"""Material scans, once-only emotion meaning, and new scheduler knobs.

No network: scans short-circuit offline; bilibili-api path is not exercised.
"""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import time

import pytest

from myatri.contracts.emotion import PAD
from myatri.persona.emotion import MEANING_FRESH_SECONDS, EmotionState
from myatry.proactive import CANDIDATE_PRIORITY, ProactiveSettings
from myatry.wechat import WeChatRunner  # noqa: F401  (ensure transport imports)

from test_wechat import make_bridge

NOW = datetime.now(timezone.utc)


def emotion_state(meaning_age_seconds, *, distance=0.4):
    observed = (NOW - timedelta(seconds=meaning_age_seconds)).isoformat()
    return {"enabled": True, "algorithm": "emotion-v1", "snapshot": None,
            "evaluated_at": NOW.isoformat(),
            "affect": PAD(valence=0.5 + distance, arousal=0.5, dominance=0.5).model_dump(),
            "mood": PAD(valence=0.5, arousal=0.5, dominance=0.5).model_dump(),
            "baseline": PAD(valence=0.5, arousal=0.5, dominance=0.5).model_dump(),
            "last_observed_at": observed, "appraisal_refs": ["a@1"], "open_concern_refs": [],
            "last_appraisal": {"meaning": "被夸了，有点开心"}}


def test_emotion_meaning_shown_once_then_gated():
    emotions = EmotionState(None)
    fresh = emotions.context_block(emotion_state(60))
    assert "被夸了" in fresh and "同一次变化" in fresh
    stale = emotions.context_block(emotion_state(MEANING_FRESH_SECONDS + 60))
    assert "被夸了" not in stale and "情绪连续性" in stale


def test_settings_accept_material_keys_with_conservative_defaults():
    settings = ProactiveSettings.from_config({
        "web_scan_minutes": 120, "bili_scan_minutes": 180,
        "bili_scan_keywords": ["游戏"], "bili_min_plays": 5000,
    })
    assert settings.web_scan_minutes == 120.0 and settings.bili_scan_minutes == 180.0
    assert settings.bili_scan_keywords == ("游戏",) and settings.bili_min_plays == 5000
    default = ProactiveSettings.from_config()
    assert default.web_scan_minutes == 120.0 and default.bili_scan_minutes == 180.0
    assert "web_curiosity" in CANDIDATE_PRIORITY and "bili_duty" in CANDIDATE_PRIORITY


@pytest.fixture
def bridge(tmp_path):
    bridge, _ = make_bridge(tmp_path)
    bridge.proactive_config = {"web_scan_minutes": 120.0, "bili_scan_minutes": 180.0}
    return bridge


def insert_material(bridge, scope, kind, created, payload):
    with sqlite3.connect(bridge.path) as db:
        return db.execute(
            "INSERT INTO proactive_materials(scope,kind,created,topic,payload) VALUES (?,?,?,?,?)",
            (scope, kind, created, "话题", json.dumps(payload, ensure_ascii=False))).lastrowid


def test_material_due_gating(bridge):
    scope = "s" * 32
    assert bridge._material_due(scope, "web_curiosity", 120.0) is True
    insert_material(bridge, scope, "web_curiosity", time.time() - 60, {"query": "x"})
    assert bridge._material_due(scope, "web_curiosity", 120.0) is False
    assert bridge._material_due(scope, "web_curiosity", 0.0) is False
    insert_material(bridge, scope, "bili_duty", time.time() - 3 * 3600, {})
    assert bridge._material_due(scope, "bili_duty", 180.0) is True


async def test_scans_skip_offline_and_candidates_use_newest_per_kind(bridge):
    scope = "s" * 32
    assert bridge._scan_web_material(scope) is None            # offline mode
    assert await bridge._scan_bili_material(scope) is None     # offline mode
    insert_material(bridge, scope, "web_curiosity", time.time() - 60,
                    {"query": "烤肉", "results": "附近烤肉店排名..."})
    insert_material(bridge, scope, "bili_duty", time.time() - 30,
                    {"keyword": "游戏", "videos": [{"bvid": "BV1", "title": "新作实机", "plays": 12000}]})
    found = await bridge._material_candidates(scope, NOW, set())
    kinds = {candidate.kind for candidate in found}
    assert kinds == {"web_curiosity", "bili_duty"}
    bili = next(c for c in found if c.kind == "bili_duty")
    assert "新作实机" in bili.excerpt and "12000" in bili.excerpt
    consumed = {candidate.candidate_id for candidate in found}
    assert await bridge._material_candidates(scope, NOW, consumed) == []
