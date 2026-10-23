"""Read-only model projections, separate from transport and archival envelopes."""
from copy import deepcopy


def message_view(message):
    # Keep names beside IDs: a name alone cannot distinguish same-name members.
    fields = ("message_id", "sender_id", "sender_name", "sent_at", "text",
              "reply_to_id", "quoted_message", "quote_resolution", "mentioned_ids")
    result = {key: deepcopy(message[key]) for key in fields
              if key in message and (key in {"sender_id", "text"}
                                     or message[key] not in (None, "", []))}
    if message.get("role") == "assistant" or (message.get("bot_id")
                                               and message.get("sender_id") == message["bot_id"]):
        result["role"] = "assistant"
    elif message.get("role") not in (None, "", "user"):
        result["role"] = message["role"]
    if message.get("message_type") not in (None, "", "text"):
        result["message_type"] = message["message_type"]
    if message.get('transport') == 'local_cli':
        result['transport'] = 'local_cli'
    return result


def identity_view(context):
    result = {}
    owner = context.get("owner")
    if isinstance(owner, dict):
        result["owner"] = {key: deepcopy(owner[key]) for key in
                           ("member_id", "display_name", "verified", "verification") if key in owner}
    members = []
    for member in context.get("members", []):
        row = {key: deepcopy(member[key]) for key in
               ("member_id", "display_name", "source_message_ids") if key in member}
        aliases = [name for name in member.get("aliases", []) if name != member.get("display_name")]
        if aliases:
            row["aliases"] = aliases
        members.append(row)
    if members:
        result["members"] = members
    if context.get("legacy_candidates"):
        result["legacy_candidates"] = deepcopy(context["legacy_candidates"])
    claims = context.get("current_member_claims", {})
    if claims.get("history") or claims.get("active"):
        history = list(claims.get("history", []))
        known = {row["ref"] for row in history}
        history.extend(row for row in claims.get("active", []) if row["ref"] not in known)
        result["current_member_claims"] = {
            "subject_id": claims.get("subject_id"), "history": deepcopy(history),
            "active_refs": [row["ref"] for row in claims.get("active", [])],
        }
    if context.get("cold_archive", {}).get("retention_days"):
        result["cold_archive"] = deepcopy(context["cold_archive"])
    return result


def summary_view(summaries):
    result = []
    for summary in summaries:
        container = {key: deepcopy(value) for key, value in summary.items() if key != "items"}
        rows = []
        for item in summary.get("items", []):
            row = {key: deepcopy(value) for key, value in item.items()
                   if key != "attention_score" and not
                   (key in {"window_end", "created_at"} and value == summary.get(key))}
            rows.append(row)
        container["items"] = rows
        result.append(container)
    return result
