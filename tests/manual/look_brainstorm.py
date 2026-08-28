"""Open a workspace, click Brainstorm, and photograph what a reader sees.

Not a test -- a way to look. Boots an in-process chat server on a temporary
vault with a project and a small tree, drives Chrome to the brainstorm view,
and writes two screenshots: the empty screen it opens on, and the same
screen with each card drawn in turn.

    python3 tests/manual/look_brainstorm.py /tmp/shots
"""

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "hc" / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from human_compact.trajectory import chat_state  # noqa: E402
from human_compact.trajectory import goals as GM  # noqa: E402
from human_compact.trajectory import ui  # noqa: E402

from test_chat_ui_server import browser_executable  # noqa: E402


CARDS = {
    "questions": {
        "ok": True, "card": "questions",
        "say": "Two things I could not settle from what you said.",
        "questions": {"eyebrow": "two questions", "items": [
            {"id": "who", "type": "mcq", "title": "Which rail is the problem?",
             "subtitle": "", "placeholder": "",
             "options": [{"label": "The TODO rail",
                          "why": "it is what you look at while building"},
                         {"label": "The goals tree",
                          "why": "it is what you look at while deciding"}]},
            {"id": "what", "type": "open",
             "title": "What does it stop you doing?", "subtitle": "",
             "placeholder": "the thing you gave up on…", "options": []}]}},
    "focus": {
        "ok": True, "card": "focus",
        "say": "There are two readings of this and they lead different places.",
        "focus": {"title": "Which one do you mean?", "options": [
            {"label": "The rail is too dense",
             "why": "fewer things on screen, same information"},
            {"label": "The rail is in the wrong order",
             "why": "same things, arranged the way the work goes"}]}},
    "offer": {"ok": True, "card": "offer", "offer": "todos",
              "say": "I think the second reading is right and I can break it"
                     " into four rows."},
    "goals": {
        "ok": True, "card": "goals",
        "say": "Two outcomes, not tasks.",
        "goals": [
            {"label": "The rail reads in the order the work goes",
             "why": "rows, then the document, then the prompt",
             "subgoals": [{"label": "Move Notes between TODOs and Prompt"},
                          {"label": "Fold the context by default"}]},
            {"label": "A narrow rail still shows every tab", "why": "",
             "subgoals": []}]},
    "todos": {
        "ok": True, "card": "todos", "say": "Four rows, in two pieces.",
        "todos": [],
        "subgoals": [
            {"label": "The tab row",
             "todos": ["Wrap the tabs onto a second line when the rail is"
                       " narrow", "Give the save stamp the space instead"]},
            {"label": "The document",
             "todos": ["Move the editor into the rail",
                       "Leave the preview in the middle"]}]},
}


def vault(root: Path) -> str:
    session = "chat-look"
    paths = chat_state.paths(session, root)
    paths.session_dir.mkdir(parents=True)
    top = GM.new_goal("g1", "Refactor how the TODOs work", origin="user")
    top["status"] = "in_progress"
    top["todo_items"] = [
        {"id": "t1", "text": "Add back the prompt editor", "status": "done"},
        {"id": "t2", "text": "Default hide the context", "status": ""}]
    kid = GM.new_goal("g11", "UI Changes", "g1", origin="user")
    onb = GM.new_goal("g2", "Onboarding", origin="user")
    doc = {"version": 1, "goals": [top, kid, onb]}
    GM.sanitize(doc)
    paths.goals.write_text(json.dumps(doc))
    paths.important.write_text(json.dumps({"items": []}))
    paths.prompts.write_text(json.dumps({"prompts": []}))
    paths.manifest.write_text(json.dumps({"cwd": str(root)}))
    chat_state.bind_project(session, str(root), root=root)
    return str(paths.session_dir)


def main(out_dir):
    from playwright.sync_api import sync_playwright

    chrome = browser_executable()
    if not chrome:
        raise SystemExit("Chrome/Chromium is not installed")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.TemporaryDirectory()
    root = Path(tmp.name)
    trajdir = vault(root)
    server = ui.ThreadingHTTPServer(("127.0.0.1", 0), ui.H)
    ui._configure_server(server, Path(trajdir), True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d" % server.server_address[1]
    try:
        with sync_playwright() as play:
            browser = play.chromium.launch(executable_path=chrome)
            page = browser.new_context(
                viewport={"width": 1500, "height": 950}).new_page()
            page.goto(url)
            page.wait_for_selector(".hc-viewtab", timeout=20000)
            page.get_by_text("Brainstorm", exact=True).first.click()
            page.wait_for_selector(".hc-brainstorm", state="visible",
                                   timeout=10000)
            time.sleep(0.6)
            page.screenshot(path=str(out / "brainstorm-open.png"))
            for name, card in CARDS.items():
                page.evaluate(
                    "(card) => { var bs = window.__hcPromptUI.brainstorm;"
                    "  var st = bs.state();"
                    "  st.msgs = st.msgs.slice(0, 1).concat("
                    "    [{role: 'you', text: 'the rail is unreadable when I"
                    " have four tabs and a narrow window'},"
                    "     {role: 'engelbart', text: card.say}]);"
                    "  st.card = card; st.thinking = false;"
                    "  bs.draw(); }", card)
                time.sleep(0.4)
                page.screenshot(path=str(out / ("brainstorm-%s.png" % name)))
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        tmp.cleanup()
    print("wrote", out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "/tmp/brainstorm-shots")
