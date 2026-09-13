"""Reproduce prompt and batching observations without running Claude or a model.

Run with the installed hc runtime's Python, or PYTHONPATH=hc/src python3.
All state is synthetic and lives in TemporaryDirectory; stdout is JSON.
"""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from unittest.mock import patch

from human_compact.trajectory import build as B, chat_state as CS, project_store as PS


def main():
    with tempfile.TemporaryDirectory(prefix="engelbart-explanation-") as tmp:
        base = Path(tmp)
        root, cwd = base / "state", base / "project"
        root.mkdir(); cwd.mkdir()
        sid = "explanation-fixture"
        p = CS.paths(sid, root)
        p.session_dir.mkdir()
        p.manifest.write_text(json.dumps({"session_id": sid, "cwd": str(cwd)}))
        PS.save_project(root, cwd, {"name": "Specification study", "objective": "Help novices discover requirements."})
        items = [
            {"id": "t001", "text": "Add a scenario editor", "depth": 0},
            {"id": "t002", "text": "Add an input field", "depth": 1},
            {"id": "t003", "text": "Add an expected-output field", "depth": 1},
            {"id": "t004", "text": "Add a Run button", "depth": 1},
            {"id": "t005", "text": "Display scenario history", "depth": 0},
        ]
        later = [
            {"id": "t006", "text": "Rename the Run button to Test scenario", "depth": 0},
            {"id": "t007", "text": "Add a Clear button", "depth": 0},
        ]
        goal = {"id": "g002", "parent_goal_id": "g001", "title": "Scenario editor", "status": "active",
                "notes": "Students must state an expected outcome before running a scenario.", "todo_items": items,
                "todos_md": "\n".join("  " * r["depth"] + "- [ ] " + r["text"] for r in items)}
        goals = {"goals": [{"id": "g001", "title": "Help novices discover requirements", "status": "active"}, goal]}
        important = {"items": []}
        picked = B.picked_with_children(items, [r["id"] for r in items])
        # Explicitly neutralize only environment overrides and process creation.
        with patch.dict(os.environ, {"HC_BUILD_LANE": "auto"}), patch("subprocess.Popen", side_effect=AssertionError("Probe must never launch a process")):
            full = B.compose_prompt(sid, goals, important, [], goal, picked, root=root)
            p.goal_context.write_text(CS._goal_context_text(sid, goals, important, []))
            estimate = B.fallback_estimate(sid, root, 5)
            goal["notes"] += "\nNEW NOTE: Keep the history after clearing the editor."
            goal["todo_items"] = [dict(r, status="done") for r in items] + later
            goal["todos_md"] = "\n".join(
                "  " * r["depth"] + ("- [x] " if r.get("status") == "done" else "- [ ] ") + r["text"]
                for r in goal["todo_items"])
            newer_full = B.compose_prompt(sid, goals, important, [], goal, B.picked_with_children(later, [r["id"] for r in later]), root=root)
            quick = B.compose_prompt(sid, goals, important, [], goal, B.picked_with_children(later, [r["id"] for r in later]), root=root, quick=True)
            probes = {
                "initial_rows": len(picked),
                "initial_protocol_ids": [r["id"] for r in picked if r["_picked"]],
                "initial_quick": B.prefer_quick(picked),
                "later_quick": B.prefer_quick(later),
                "full_includes_notes": "Students must state" in full,
                "next_full_includes_new_note": "NEW NOTE" in newer_full,
                "next_quick_includes_new_note": "NEW NOTE" in quick,
                "cold_fallback": estimate,
            }
            assert probes["initial_rows"] == 5
            assert probes["initial_protocol_ids"] == ["t001", "t005"]
            assert not probes["initial_quick"] and probes["later_quick"]
            assert probes["full_includes_notes"] and probes["next_full_includes_new_note"]
            assert not probes["next_quick_includes_new_note"]
            assert estimate["minutes"] == 20 and estimate["tokens"] >= 150000
        package = Path(B.__file__).resolve().parents[1]
        files = ["trajectory/build.py", "trajectory/ui.py", "trajectory/chat_state.py",
                 "trajectory/web/goal/actions.js", "trajectory/web/goal/services.js", "trajectory/web/goal/store.js",
                 "trajectory/agents/orchestrator.py", "trajectory/agents/overseer.py", "trajectory/agents/context.py",
                 "trajectory/agents/chat.py", "trajectory/agents/path.py", "trajectory/agents/acceptance.py",
                 "trajectory/agents/verifier.py", "trajectory/brainstorm.py", "trajectory/setup_chat.py", "trajectory/providers.py"]
        out = {"date": "2026-09-13", "scope": "Installed Engelbart 0.20.4; synthetic fixture; no model calls",
               "items": items, "later": later, "probes": probes,
               "full_prompt": full.replace(str(cwd), "/example/specification-study"),
               "later_full_prompt": newer_full.replace(str(cwd), "/example/specification-study"),
               "later_quick_prompt": quick.replace(str(cwd), "/example/specification-study"),
               "sources": {name: {"sha256": hashlib.sha256((package / name).read_bytes()).hexdigest(),
                                  "text": (package / name).read_text()} for name in files if (package / name).exists()}}
        print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
