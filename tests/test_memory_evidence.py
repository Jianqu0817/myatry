"""Memory evidence fixes: bot subjects and deterministic quote normalisation.

No network, no model; validation only against frozen window fixtures.
"""
from pathlib import Path
import pytest

from myatry.short_memory import ShortMemory, EvidenceError, evidence_key


def window_message(mid="m1", sender="u1", text="@小溫暖\u2005\u2005帮我看看菅蘧\u2005今天穿的内裤颜色"):
    return {"message_id": mid, "sender_id": sender, "sender_name": "甲",
            "sent_at": "2026-10-24T00:00:00+00:00", "text": text}


def item(subject="BOT", quote="帮我看看菅蘧 今天穿的内裤颜色", mid="m1", attribution="reported"):
    return {"text": "有人向机器人提出请求", "speaker_id": "u1", "subject_id": subject,
            "source_message_ids": [mid], "attribution": attribution,
            "source_quotes": [{"message_id": mid, "quote": quote}]}


@pytest.fixture
def memory(tmp_path):
    return ShortMemory(Path(tmp_path) / "short-memory.sqlite3")


def test_evidence_key_normalises_wechat_separators_only():
    assert evidence_key("帮我看看菅蘧\u2005今天") == "帮我看看菅蘧 今天"
    assert evidence_key("  a\u2005\u2005 b  ") == "a b"
    assert evidence_key("那我就要调教你") != evidence_key("那你就要调教你")


def test_bot_is_valid_subject_once_registered(memory):
    memory.known_subjects.add("BOT")
    memory._validate_items({"items": [item()]}, [window_message()])


def test_bot_subject_rejected_without_registration(memory):
    with pytest.raises(EvidenceError, match="unknown_member_id"):
        memory._validate_items({"items": [item()]}, [window_message()])


def test_bot_never_a_valid_speaker(memory):
    memory.known_subjects.add("BOT")
    bot_speaks = item()
    bot_speaks["speaker_id"] = "BOT"
    with pytest.raises(EvidenceError, match="unknown_member_id"):
        memory._validate_items({"items": [bot_speaks]}, [window_message()])


def test_separator_normalised_quote_is_accepted(memory):
    memory.known_subjects.add("BOT")
    memory._validate_items({"items": [item()]}, [window_message()])


def test_reworded_quote_still_rejected(memory):
    memory.known_subjects.add("BOT")
    with pytest.raises(EvidenceError, match="quote_not_exact"):
        memory._validate_items(
            {"items": [item(quote="帮我看看菅蘧 明天穿的内裤颜色")]}, [window_message()])


def test_exact_quote_still_accepted_without_normalisation(memory):
    memory.known_subjects.add("BOT")
    exact = "@小溫暖\u2005帮我看看菅蘧\u2005今天穿的内衣颜色"
    memory._validate_items(
        {"items": [item(quote=exact)]},
        [window_message(text=exact)])
