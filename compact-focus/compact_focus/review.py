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


def new_review(proposal: Dict[str, Any]) -> Dict[str, Any]:
    items = copy.deepcopy(proposal.get("items", []))
    for item in items:
        item.setdefault("reviewed", not bool(item.get("needs_review")))
        item.setdefault("origin", "proposal")
        item.setdefault("user_touched", False)
    return {
        "schema_version": SCHEMA_VERSION,
        "source_hash": proposal.get("source_hash"),
        "started_at": utc_now(),
        "completed_at": None,
        "outcome": "editing",
        "precommit": "",
        "items": items,
        "class_rules": copy.deepcopy(proposal.get("class_rules", [])),
        "actions": [],
    }


def record_action(review: Dict[str, Any], action: str, **fields: Any) -> None:
    event = {"ts": utc_now(), "action": action}
    event.update(fields)
    review.setdefault("actions", []).append(event)


def set_precommit(review: Dict[str, Any], text: str) -> None:
    review["precommit"] = text.strip()[:4000]
    record_action(review, "precommit", skipped=not bool(review["precommit"]))


def touch_item(item: Dict[str, Any], action: str = "edit") -> None:
    item["user_touched"] = True
    item["reviewed"] = True
    item["needs_review"] = False
    item["last_user_action"] = action


def set_item_field(review: Dict[str, Any], index: int, field: str, value: Any) -> None:
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
    touch_item(item, f"set_{field}")
    record_action(review, "set_item_field", item_id=item.get("id"), field=field, before=before, after=value)


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
    }
    index = len(review["items"]) if after is None else min(len(review["items"]), after + 1)
    review["items"].insert(index, item)
    record_action(review, "create_item", item_id=item["id"], source_ids=refs)
    return index


def move_source(review: Dict[str, Any], source_id: str, target_index: int) -> int:
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
    touch_item(source_item, "move_source")
    touch_item(target, "move_source")
    source_id_from = source_item.get("id")
    if not source_item["source_ids"] and source_item.get("origin") != "human":
        review["items"].remove(source_item)
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
    touch_item(target, "merge")
    removed = review["items"].pop(first)
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
    source_order: List[str] = []
    sources: Dict[str, Dict[str, Any]] = {}
    for episode in trace.get("episodes", []):
        for source in episode.get("sources", []):
            source_order.append(source["id"])
            sources[source["id"]] = source
    for item in review.get("items", []):
        if item.get("user_touched"):
            continue
        model_retention = item.get("model_retention", item.get("retention", "preserve"))
        floor = rule_floor(item.get("source_ids", []), source_order, sources, review.get("class_rules", []))
        item["rule_floor"] = floor
        item["retention"] = (
            max((model_retention, floor), key=RETENTION_WEIGHT.__getitem__) if floor else model_retention
        )


def review_errors(trace: Dict[str, Any], review: Dict[str, Any]) -> List[str]:
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
        if item.get("needs_review") and not item.get("reviewed"):
            errors.append(f"item {index} is still contested: {item.get('title', '')}")
        refs = item.get("source_ids", [])
        if not refs and item.get("origin") != "human":
            errors.append(f"item {index} has no evidence")
        for source_id in refs:
            if source_id not in expected_set:
                errors.append(f"item {index} references unknown source {source_id}")
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
    initial = {item.get("id"): item for item in proposal.get("items", [])}
    changed: List[Dict[str, Any]] = []
    for item in review.get("items", []):
        before = initial.get(item.get("id"))
        if before is None:
            changed.append({"item_id": item.get("id"), "change": "added"})
            continue
        fields: Dict[str, Any] = {}
        for field in ("title", "summary", "type", "status", "retention", "source_ids"):
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
        "action_count": len(review.get("actions", [])),
        "actions": copy.deepcopy(review.get("actions", [])),
    }


def review_hash(review: Dict[str, Any]) -> str:
    material = {
        "source_hash": review.get("source_hash"),
        "precommit": review.get("precommit", ""),
        "items": [
            {key: item.get(key) for key in ("id", "title", "summary", "type", "status", "retention", "source_ids", "next_step")}
            for item in review.get("items", [])
        ],
        "class_rules": review.get("class_rules", []),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
