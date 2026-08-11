from __future__ import annotations

import copy
import hashlib
import json
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import SCHEMA_VERSION
from .proposal import (
    CONFIDENCES,
    ITEM_TYPES,
    RETENTIONS,
    STATUSES,
    RETENTION_WEIGHT,
    rule_floor,
)
from .state import utc_now


WORK_STATES = ("todo", "in_progress", "done", "blocked")
STATUS_WORK_STATE = {
    "active": "in_progress",
    "resolved": "done",
    "unclear": "todo",
}


def _work_state(item: Dict[str, Any]) -> str:
    value = str(item.get("work_state") or "")
    if value in WORK_STATES:
        return value
    return STATUS_WORK_STATE.get(str(item.get("status") or "unclear"), "todo")


def ensure_review_shape(review: Dict[str, Any]) -> None:
    """Upgrade older drafts in memory without invalidating their source hash."""
    source_reviews = review.setdefault("source_reviews", {})
    if not isinstance(source_reviews, dict):
        source_reviews = {}
        review["source_reviews"] = source_reviews
    for item in review.get("items", []):
        item.setdefault("work_state", _work_state(item))
        item.setdefault("clarifications", [])
        item.setdefault("retention_touched", bool(item.get("user_touched")))
        for source_id in item.get("source_ids", []):
            state = source_reviews.setdefault(
                source_id,
                {
                    "retention": item.get("retention", "preserve"),
                    "model_retention": item.get("retention", "preserve"),
                    "work_state": item["work_state"],
                    "clarifications": [],
                    "user_touched": False,
                    "retention_touched": False,
                    "work_state_touched": False,
                },
            )
            if not isinstance(state, dict):
                state = {}
                source_reviews[source_id] = state
            state.setdefault("retention", item.get("retention", "preserve"))
            state.setdefault("model_retention", state.get("retention", item.get("retention", "preserve")))
            state.setdefault("work_state", item["work_state"])
            state.setdefault("clarifications", [])
            state.setdefault("user_touched", False)
            state.setdefault("retention_touched", bool(state.get("user_touched")))
            state.setdefault("work_state_touched", bool(state.get("user_touched")))
    review.setdefault("draft_review", {})
    review.setdefault("approved_summary", "")


def new_review(proposal: Dict[str, Any]) -> Dict[str, Any]:
    items = copy.deepcopy(proposal.get("items", []))
    for item in items:
        item.setdefault("reviewed", not bool(item.get("needs_review")))
        item.setdefault("origin", "proposal")
        item.setdefault("user_touched", False)
        item.setdefault("work_state", _work_state(item))
        item.setdefault("clarifications", [])
    review = {
        "schema_version": SCHEMA_VERSION,
        "source_hash": proposal.get("source_hash"),
        "started_at": utc_now(),
        "completed_at": None,
        "outcome": "editing",
        "precommit": "",
        "items": items,
        "class_rules": copy.deepcopy(proposal.get("class_rules", [])),
        "source_reviews": {},
        "draft_review": {},
        "approved_summary": "",
        "actions": [],
    }
    ensure_review_shape(review)
    return review


def record_action(review: Dict[str, Any], action: str, **fields: Any) -> None:
    event = {"ts": utc_now(), "action": action}
    event.update(fields)
    review.setdefault("actions", []).append(event)


def invalidate_draft(review: Dict[str, Any]) -> None:
    review["approved_summary"] = ""
    draft_review = review.setdefault("draft_review", {})
    if isinstance(draft_review, dict) and draft_review.get("draft"):
        draft_review["stale"] = True
        draft_review["approved"] = False


def set_precommit(review: Dict[str, Any], text: str) -> None:
    review["precommit"] = text.strip()[:4000]
    invalidate_draft(review)
    record_action(review, "precommit", skipped=not bool(review["precommit"]))


def touch_item(item: Dict[str, Any], action: str = "edit") -> None:
    item["user_touched"] = True
    item["reviewed"] = True
    item["needs_review"] = False
    item["last_user_action"] = action


def set_item_field(review: Dict[str, Any], index: int, field: str, value: Any) -> None:
    ensure_review_shape(review)
    item = review["items"][index]
    allowed = {
        "title": None,
        "summary": None,
        "next_step": None,
        "retention": RETENTIONS,
        "status": STATUSES,
        "type": ITEM_TYPES,
        "confidence": CONFIDENCES,
    }
    if field not in allowed:
        raise ValueError(f"field is not editable: {field}")
    if allowed[field] is not None and value not in allowed[field]:
        raise ValueError(f"invalid {field}: {value}")
    if field in {"title", "summary", "next_step"}:
        value = str(value).strip()
        if field == "title" and not value:
            raise ValueError("title cannot be empty")
    before = item.get(field)
    item[field] = value
    if field == "retention":
        item["retention_touched"] = True
        for source_id in item.get("source_ids", []):
            source_review = review["source_reviews"][source_id]
            if not source_review.get("retention_touched"):
                source_review["retention"] = value
    touch_item(item, f"set_{field}")
    invalidate_draft(review)
    record_action(review, "set_item_field", item_id=item.get("id"), field=field, before=before, after=value)


def set_work_state(
    review: Dict[str, Any],
    index: int,
    value: str,
    *,
    source_id: Optional[str] = None,
) -> None:
    ensure_review_shape(review)
    if value not in WORK_STATES:
        raise ValueError(f"invalid work state: {value}")
    item = review["items"][index]
    if source_id is None:
        before = item.get("work_state")
        item["work_state"] = value
        for candidate_id in item.get("source_ids", []):
            source_review = review["source_reviews"][candidate_id]
            if not source_review.get("work_state_touched"):
                source_review["work_state"] = value
        touch_item(item, "set_work_state")
        target = item.get("id")
    else:
        if source_id not in item.get("source_ids", []):
            raise ValueError(f"source {source_id} is not in cluster {item.get('id')}")
        source_review = review["source_reviews"][source_id]
        before = source_review.get("work_state")
        source_review["work_state"] = value
        source_review["user_touched"] = True
        source_review["work_state_touched"] = True
        touch_item(item, "set_source_work_state")
        target = source_id
    invalidate_draft(review)
    record_action(review, "set_work_state", target_id=target, before=before, after=value)


def set_source_retention(review: Dict[str, Any], index: int, source_id: str, value: str) -> None:
    ensure_review_shape(review)
    if value not in RETENTIONS:
        raise ValueError(f"invalid retention: {value}")
    item = review["items"][index]
    if source_id not in item.get("source_ids", []):
        raise ValueError(f"source {source_id} is not in cluster {item.get('id')}")
    source_review = review["source_reviews"][source_id]
    before = source_review.get("retention")
    source_review["retention"] = value
    source_review["user_touched"] = True
    source_review["retention_touched"] = True
    touch_item(item, "set_source_retention")
    invalidate_draft(review)
    record_action(
        review,
        "set_source_retention",
        item_id=item.get("id"),
        source_id=source_id,
        before=before,
        after=value,
    )


def effective_source_retention(review: Dict[str, Any], item: Dict[str, Any], source_id: str) -> str:
    if not isinstance(review.get("source_reviews"), dict):
        ensure_review_shape(review)
    state = review.get("source_reviews", {}).get(source_id) or {}
    value = str(state.get("retention") or item.get("retention") or "preserve")
    return value if value in RETENTIONS else "preserve"


def add_clarification(
    review: Dict[str, Any],
    index: int,
    text: str,
    *,
    source_id: Optional[str] = None,
) -> None:
    ensure_review_shape(review)
    value = str(text).strip()[:4000]
    if not value:
        return
    item = review["items"][index]
    if source_id is None:
        item.setdefault("clarifications", []).append(value)
        touch_item(item, "clarify")
        target = item.get("id")
    else:
        if source_id not in item.get("source_ids", []):
            raise ValueError(f"source {source_id} is not in cluster {item.get('id')}")
        source_review = review["source_reviews"][source_id]
        source_review.setdefault("clarifications", []).append(value)
        source_review["user_touched"] = True
        touch_item(item, "clarify_source")
        target = source_id
    invalidate_draft(review)
    record_action(review, "clarify", target_id=target, text=value)


def resolve_item(review: Dict[str, Any], index: int, rival_index: Optional[int] = None) -> None:
    item = review["items"][index]
    if rival_index is not None:
        rivals = item.get("rival_interpretations") or []
        if rival_index < 0 or rival_index >= len(rivals):
            raise IndexError(rival_index)
        before = item.get("summary")
        item["summary"] = str(rivals[rival_index])
        record_action(
            review,
            "choose_rival",
            item_id=item.get("id"),
            rival_index=rival_index,
            before=before,
            after=item["summary"],
        )
    touch_item(item, "resolve")
    invalidate_draft(review)
    record_action(review, "resolve_item", item_id=item.get("id"))


def _new_id(title: str, source_ids: Sequence[str], salt: str = "") -> str:
    identity = title + "\x1f" + "\x1f".join(source_ids) + "\x1e" + salt
    return "i-human-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]


def create_item(
    review: Dict[str, Any],
    title: str,
    *,
    source_ids: Optional[Sequence[str]] = None,
    retention: str = "preserve",
    after: Optional[int] = None,
) -> int:
    title = title.strip()[:120]
    if not title:
        raise ValueError("title cannot be empty")
    refs = list(dict.fromkeys(source_ids or []))
    item = {
        "id": _new_id(title, refs, str(len(review.get("actions", [])))),
        "title": title,
        "summary": title,
        "type": "context",
        "status": "active",
        "retention": retention if retention in RETENTIONS else "preserve",
        "model_retention": retention if retention in RETENTIONS else "preserve",
        "confidence": "high",
        "needs_review": False,
        "reviewed": True,
        "source_ids": refs,
        "rationale": "Added during human review.",
        "next_step": "",
        "rival_interpretations": [],
        "rule_floor": None,
        "user_touched": True,
        "origin": "human",
        "work_state": "in_progress",
        "clarifications": [],
        "retention_touched": True,
    }
    index = len(review["items"]) if after is None else min(len(review["items"]), after + 1)
    review["items"].insert(index, item)
    ensure_review_shape(review)
    invalidate_draft(review)
    record_action(review, "create_item", item_id=item["id"], source_ids=refs)
    return index


def move_source(review: Dict[str, Any], source_id: str, target_index: int) -> int:
    ensure_review_shape(review)
    source_item: Optional[Dict[str, Any]] = None
    for item in review["items"]:
        if source_id in item.get("source_ids", []):
            source_item = item
            break
    if source_item is None:
        raise ValueError(f"unknown source: {source_id}")
    target = review["items"][target_index]
    if source_item is target:
        return target_index
    source_item["source_ids"].remove(source_id)
    target.setdefault("source_ids", []).append(source_id)
    source_review = review["source_reviews"].get(source_id, {})
    if not source_review.get("retention_touched"):
        source_review["retention"] = target.get("retention", "preserve")
    if not source_review.get("work_state_touched"):
        source_review["work_state"] = target.get("work_state", _work_state(target))
    touch_item(source_item, "move_source")
    touch_item(target, "move_source")
    source_id_from = source_item.get("id")
    if not source_item["source_ids"] and source_item.get("origin") != "human":
        review["items"].remove(source_item)
    invalidate_draft(review)
    record_action(
        review,
        "move_source",
        source_id=source_id,
        from_item=source_id_from,
        to_item=target.get("id"),
    )
    return review["items"].index(target)


def split_source(review: Dict[str, Any], source_id: str, title: str, after: int) -> int:
    index = create_item(review, title, retention=review["items"][after].get("retention", "preserve"), after=after)
    item_id = review["items"][index]["id"]
    final_index = move_source(review, source_id, index)
    record_action(review, "split_source", source_id=source_id, item_id=item_id)
    return final_index


def merge_items(review: Dict[str, Any], first: int, second: int) -> int:
    ensure_review_shape(review)
    if first == second:
        return first
    source = review["items"][first]
    target = review["items"][second]
    target["source_ids"] = list(dict.fromkeys(target.get("source_ids", []) + source.get("source_ids", [])))
    if source.get("summary") and source["summary"] not in target.get("summary", ""):
        target["summary"] = (target.get("summary", "").rstrip() + "\n\n" + source["summary"].strip()).strip()
    target["retention"] = max(
        (target.get("retention", "preserve"), source.get("retention", "preserve")),
        key=RETENTION_WEIGHT.__getitem__,
    )
    target["retention_touched"] = True
    for source_id in target.get("source_ids", []):
        source_review = review["source_reviews"].get(source_id, {})
        if not source_review.get("retention_touched"):
            source_review["retention"] = target["retention"]
        if not source_review.get("work_state_touched"):
            source_review["work_state"] = target.get("work_state", _work_state(target))
    touch_item(target, "merge")
    removed = review["items"].pop(first)
    invalidate_draft(review)
    record_action(review, "merge_items", into=target.get("id"), removed=removed.get("id"))
    return review["items"].index(target)


def toggle_rule(review: Dict[str, Any], rule_index: int, trace: Dict[str, Any]) -> None:
    rule = review["class_rules"][rule_index]
    rule["enabled"] = not bool(rule.get("enabled"))
    record_action(review, "toggle_rule", rule_id=rule.get("id"), enabled=rule["enabled"])
    reapply_rules(review, trace)


def change_first_percent(review: Dict[str, Any], delta: int, trace: Dict[str, Any]) -> None:
    rule = next((value for value in review["class_rules"] if value.get("id") == "first_n"), None)
    if not rule:
        return
    before = int(rule.get("percent", 30))
    rule["percent"] = max(0, min(100, before + delta))
    record_action(review, "change_rule", rule_id="first_n", before=before, after=rule["percent"])
    reapply_rules(review, trace)


def reapply_rules(review: Dict[str, Any], trace: Dict[str, Any]) -> None:
    ensure_review_shape(review)
    source_order: List[str] = []
    sources: Dict[str, Dict[str, Any]] = {}
    for episode in trace.get("episodes", []):
        for source in episode.get("sources", []):
            source_order.append(source["id"])
            sources[source["id"]] = source
    for item in review.get("items", []):
        if item.get("retention_touched"):
            continue
        model_retention = item.get("model_retention", item.get("retention", "preserve"))
        floor = rule_floor(item.get("source_ids", []), source_order, sources, review.get("class_rules", []))
        item["rule_floor"] = floor
        item["retention"] = (
            max((model_retention, floor), key=RETENTION_WEIGHT.__getitem__) if floor else model_retention
        )
        for source_id in item.get("source_ids", []):
            source_review = review["source_reviews"][source_id]
            if not source_review.get("retention_touched"):
                source_review["retention"] = item["retention"]
    invalidate_draft(review)


def review_errors(trace: Dict[str, Any], review: Dict[str, Any]) -> List[str]:
    ensure_review_shape(review)
    errors: List[str] = []
    expected = [
        source["id"]
        for episode in trace.get("episodes", [])
        for source in episode.get("sources", [])
        if source.get("id")
    ]
    expected_set = set(expected)
    seen: List[str] = []
    item_ids: List[str] = []
    for index, item in enumerate(review.get("items", []), 1):
        item_id = str(item.get("id") or "")
        if not item_id:
            errors.append(f"item {index} has no id")
        item_ids.append(item_id)
        if not str(item.get("title") or "").strip():
            errors.append(f"item {index} has no title")
        if item.get("retention") not in RETENTIONS:
            errors.append(f"item {index} has invalid retention")
        if item.get("type") not in ITEM_TYPES:
            errors.append(f"item {index} has invalid type")
        if item.get("status") not in STATUSES:
            errors.append(f"item {index} has invalid status")
        if item.get("work_state") not in WORK_STATES:
            errors.append(f"item {index} has invalid work state")
        if item.get("needs_review") and not item.get("reviewed"):
            errors.append(f"item {index} is still contested: {item.get('title', '')}")
        refs = item.get("source_ids", [])
        if not refs and item.get("origin") != "human":
            errors.append(f"item {index} has no evidence")
        for source_id in refs:
            if source_id not in expected_set:
                errors.append(f"item {index} references unknown source {source_id}")
            source_review = review.get("source_reviews", {}).get(source_id)
            if not isinstance(source_review, dict):
                errors.append(f"source {source_id} has no review state")
            else:
                if source_review.get("retention") not in RETENTIONS:
                    errors.append(f"source {source_id} has invalid retention")
                if source_review.get("work_state") not in WORK_STATES:
                    errors.append(f"source {source_id} has invalid work state")
            seen.append(source_id)
    if len(item_ids) != len(set(item_ids)):
        errors.append("item ids are not unique")
    duplicates = sorted({source_id for source_id in seen if seen.count(source_id) > 1})
    missing = [source_id for source_id in expected if source_id not in seen]
    if duplicates:
        errors.append("sources assigned more than once: " + ", ".join(duplicates[:8]))
    if missing:
        errors.append("sources not assigned: " + ", ".join(missing[:8]))
    return errors


def review_delta(proposal: Dict[str, Any], review: Dict[str, Any]) -> Dict[str, Any]:
    ensure_review_shape(review)
    initial = {item.get("id"): item for item in proposal.get("items", [])}
    changed: List[Dict[str, Any]] = []
    for item in review.get("items", []):
        before = initial.get(item.get("id"))
        if before is None:
            changed.append({"item_id": item.get("id"), "change": "added"})
            continue
        fields: Dict[str, Any] = {}
        for field in (
            "title",
            "summary",
            "type",
            "status",
            "work_state",
            "retention",
            "source_ids",
            "clarifications",
        ):
            if before.get(field) != item.get(field):
                fields[field] = {"before": before.get(field), "after": item.get(field)}
        if fields:
            changed.append({"item_id": item.get("id"), "change": "edited", "fields": fields})
    final_ids = {item.get("id") for item in review.get("items", [])}
    for item_id in initial:
        if item_id not in final_ids:
            changed.append({"item_id": item_id, "change": "removed_or_merged"})
    return {
        "schema_version": SCHEMA_VERSION,
        "source_hash": review.get("source_hash"),
        "precommit": review.get("precommit", ""),
        "changed_items": changed,
        "source_reviews": copy.deepcopy(review.get("source_reviews", {})),
        "approved_summary": review.get("approved_summary", ""),
        "draft_review": copy.deepcopy(review.get("draft_review", {})),
        "action_count": len(review.get("actions", [])),
        "actions": copy.deepcopy(review.get("actions", [])),
    }


def review_hash(review: Dict[str, Any]) -> str:
    ensure_review_shape(review)
    material = {
        "source_hash": review.get("source_hash"),
        "precommit": review.get("precommit", ""),
        "items": [
            {
                key: item.get(key)
                for key in (
                    "id",
                    "title",
                    "summary",
                    "type",
                    "status",
                    "work_state",
                    "retention",
                    "source_ids",
                    "next_step",
                    "clarifications",
                    "retention_touched",
                )
            }
            for item in review.get("items", [])
        ],
        "source_reviews": review.get("source_reviews", {}),
        "approved_summary": review.get("approved_summary", ""),
        "draft_review": review.get("draft_review", {}),
        "class_rules": review.get("class_rules", []),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
