"""The account switch: reading which credential `claude` runs on, and moving
it between the Engelbart pool and the member's own login without touching
anything that is not ours."""
import json
import sys
from pathlib import Path

from human_compact import claude_account as CA
from human_compact import credential_helper as CH


GATEWAY = "https://gw.example"
API_BASE = "https://api.example"


def sh_helper(tmp_path, credentials, settings):
    """A helper script shaped exactly like engelbart's managedHelperSource."""
    script = tmp_path / "root" / "bin" / "engelbart-key"
    script.parent.mkdir(parents=True, exist_ok=True)
    words = " ".join(f"'{w}'" for w in (
        "python", "-m", "human_compact.credential_helper",
        "--credentials", str(credentials), "--settings", str(settings),
        "--helper", str(script), "--base-url", GATEWAY))
    script.write_text("#!/bin/sh\n# Written by Engelbart.\n"
                      f"exec {words} \"$@\"\n", encoding="utf-8")
    return script


def write_credentials(tmp_path, **claude):
    record = {"schema": 1, "token": "device-token", "apiBase": API_BASE,
              "claude": {"baseUrl": GATEWAY, "budgetUsd": 25.0,
                         "spendUsd": 2.5, **claude}}
    where = tmp_path / "root" / "auth.json"
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps(record), encoding="utf-8")
    return where


def env_for(tmp_path):
    # HUMAN_COMPACT_HOME pins the managed-root probe inside the test tree, so
    # a developer machine's real ~/.human-compact never leaks into a test.
    return {"CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
            "HUMAN_COMPACT_HOME": str(tmp_path / "root")}


def settings_file(tmp_path):
    return tmp_path / "claude-config" / "settings.json"


def wired_settings(tmp_path, script):
    where = settings_file(tmp_path)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps({
        "theme": "dark",
        "apiKeyHelper": str(script),
        "env": {"ANTHROPIC_BASE_URL": GATEWAY,
                "CLAUDE_CODE_API_KEY_HELPER_TTL_MS": CH.HELPER_TTL_MS},
    }, indent=2) + "\n", encoding="utf-8")
    return where


def point_prefix_at(monkeypatch, tmp_path):
    # _find_helper walks up from the managed venv; the fake install's bin/
    # sits two levels above the pretend prefix.
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "root" / "runtime" / "venv"))


def test_settings_path_honors_claude_config_dir(tmp_path):
    env = env_for(tmp_path)
    assert CA.settings_path(env) == settings_file(tmp_path)
    assert CA.settings_path({}) == Path.home() / ".claude" / "settings.json"


def test_helper_script_recognizes_ours_and_only_ours(tmp_path):
    posix = tmp_path / "bin" / "engelbart-key"
    assert CA._helper_script(str(posix)) == posix
    windows = tmp_path / "bin" / "engelbart-key.ps1"
    value = f'powershell -NoProfile -ExecutionPolicy Bypass -File "{windows}"'
    assert CA._helper_script(value) == windows
    assert CA._helper_value(windows) == value
    assert CA._helper_script("/somewhere/else/key-helper") is None
    assert CA._helper_script("") is None


def test_script_args_parse_both_helper_shapes(tmp_path):
    credentials = write_credentials(tmp_path)
    script = sh_helper(tmp_path, credentials, settings_file(tmp_path))
    args = CA._script_args(script)
    assert args["credentials"] == str(credentials)
    assert args["base-url"] == GATEWAY
    # The ps1 twin quotes values but not flags.
    ps1 = tmp_path / "root" / "bin" / "engelbart-key.ps1"
    ps1.write_text("$ErrorActionPreference = \"Stop\"\n"
                   f"& 'python' -m human_compact.credential_helper "
                   f"--credentials '{credentials}' --settings 'x' "
                   f"--helper 'y' --base-url '{GATEWAY}' @args\n",
                   encoding="utf-8")
    assert CA._script_args(ps1)["credentials"] == str(credentials)


def test_status_reads_own_account_when_nothing_is_wired(tmp_path, monkeypatch):
    point_prefix_at(monkeypatch, tmp_path)
    state = CA.status(env_for(tmp_path))
    assert state["ok"] and state["using"] == "own"
    assert not state["wired"] and not state["available"]


def test_status_reads_the_pool_when_wired(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    script = sh_helper(tmp_path, credentials, settings_file(tmp_path))
    wired_settings(tmp_path, script)
    point_prefix_at(monkeypatch, tmp_path)
    state = CA.status(env_for(tmp_path))
    assert state["using"] == "engelbart" and state["wired"]
    assert state["available"]
    assert state["budget_usd"] == 25.0 and state["spend_usd"] == 2.5
    assert "apiKey" not in json.dumps(state)


def test_status_leaves_a_foreign_helper_alone(tmp_path):
    where = settings_file(tmp_path)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps({"apiKeyHelper": "/their/own/helper"}),
                     encoding="utf-8")
    state = CA.status(env_for(tmp_path))
    assert state["using"] == "own" and state["foreign_helper"]


def test_status_fresh_asks_the_server_and_strips_the_key(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    script = sh_helper(tmp_path, credentials, settings_file(tmp_path))
    wired_settings(tmp_path, script)
    point_prefix_at(monkeypatch, tmp_path)
    monkeypatch.setattr(CH, "_request", lambda record: (
        200, {"apiKey": "sk-live-secret", "budgetUsd": 25.0,
              "spendUsd": 9.75, "status": "ready"}, ""))
    state = CA.status(env_for(tmp_path), fresh=True)
    assert state["spend_usd"] == 9.75
    assert state["credit_status"] == "ready"
    assert "sk-live-secret" not in json.dumps(state)


def test_switch_to_own_unwires_and_keeps_the_rest(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    script = sh_helper(tmp_path, credentials, settings_file(tmp_path))
    where = wired_settings(tmp_path, script)
    point_prefix_at(monkeypatch, tmp_path)
    state = CA.switch("own", env_for(tmp_path))
    assert state["ok"] and state["using"] == "own"
    parsed = json.loads(where.read_text(encoding="utf-8"))
    assert "apiKeyHelper" not in parsed
    assert "env" not in parsed
    assert parsed["theme"] == "dark"


def test_switch_to_own_when_already_there_is_a_success(tmp_path, monkeypatch):
    point_prefix_at(monkeypatch, tmp_path)
    assert CA.switch("own", env_for(tmp_path))["ok"]


def test_switch_to_engelbart_wires_what_the_cli_would(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    script = sh_helper(tmp_path, credentials, settings_file(tmp_path))
    where = settings_file(tmp_path)
    where.parent.mkdir(parents=True, exist_ok=True)
    where.write_text(json.dumps({"theme": "dark"}), encoding="utf-8")
    point_prefix_at(monkeypatch, tmp_path)
    monkeypatch.setattr(CH, "_request", lambda record: (
        200, {"apiKey": "sk-live-secret", "baseUrl": GATEWAY,
              "budgetUsd": 25.0, "spendUsd": 2.5, "status": "ready"}, ""))
    state = CA.switch("engelbart", env_for(tmp_path))
    assert state["ok"] and state["using"] == "engelbart"
    assert "sk-live-secret" not in json.dumps(state)
    parsed = json.loads(where.read_text(encoding="utf-8"))
    assert parsed["apiKeyHelper"] == str(script)
    assert parsed["env"]["ANTHROPIC_BASE_URL"] == GATEWAY
    assert parsed["env"]["CLAUDE_CODE_API_KEY_HELPER_TTL_MS"] == CH.HELPER_TTL_MS
    assert parsed["theme"] == "dark"
    # And the credential helper's own unwire recognises the wiring as its.
    assert CH.unwire(where, str(script), GATEWAY)


def test_switch_to_engelbart_refuses_a_spent_key(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    sh_helper(tmp_path, credentials, settings_file(tmp_path))
    point_prefix_at(monkeypatch, tmp_path)
    monkeypatch.setattr(CH, "_request", lambda record: (
        200, {"apiKey": "sk-dead", "status": "exhausted"}, ""))
    state = CA.switch("engelbart", env_for(tmp_path))
    assert not state["ok"] and "used up" in state["error"]
    assert not settings_file(tmp_path).exists()


def test_switch_to_engelbart_refuses_without_an_install(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere" / "venv"))
    state = CA.switch("engelbart", env_for(tmp_path))
    assert not state["ok"] and "engelbart auth" in state["error"]


def test_switch_never_touches_a_foreign_helper(tmp_path, monkeypatch):
    credentials = write_credentials(tmp_path)
    sh_helper(tmp_path, credentials, settings_file(tmp_path))
    where = settings_file(tmp_path)
    where.parent.mkdir(parents=True, exist_ok=True)
    before = json.dumps({"apiKeyHelper": "/their/own/helper"}, indent=2)
    where.write_text(before, encoding="utf-8")
    point_prefix_at(monkeypatch, tmp_path)
    for which in ("own", "engelbart"):
        state = CA.switch(which, env_for(tmp_path))
        assert not state["ok"]
        assert where.read_text(encoding="utf-8") == before


def test_switch_refuses_an_unknown_choice(tmp_path):
    assert not CA.switch("sideways", env_for(tmp_path))["ok"]


def test_status_names_the_dashboard(tmp_path, monkeypatch):
    write_credentials(tmp_path)
    sh_helper(tmp_path, tmp_path / "root" / "auth.json", settings_file(tmp_path))
    point_prefix_at(monkeypatch, tmp_path)
    assert CA.status(env_for(tmp_path))["dashboard"] == API_BASE + "/engelbart"


def answer(status):
    return lambda record: (200, {"apiKey": "sk-live-secret",
                                 "status": status}, "")


def test_credit_alert_fires_once_per_exhaustion(tmp_path, monkeypatch):
    write_credentials(tmp_path)
    sh_helper(tmp_path, tmp_path / "root" / "auth.json", settings_file(tmp_path))
    traj = tmp_path / "traj"
    env = env_for(tmp_path)
    monkeypatch.setattr(CH, "_request", answer("active"))
    assert CA.credit_alert(traj, env, now=0) is None
    # Within the check window nothing is asked, whatever the pool says.
    monkeypatch.setattr(CH, "_request", answer("exhausted"))
    assert CA.credit_alert(traj, env, now=CA.CREDIT_CHECK_SECONDS - 1) is None
    # Due again: the transition to exhausted is the event, said once.
    alert = CA.credit_alert(traj, env, now=CA.CREDIT_CHECK_SECONDS + 1)
    assert alert and alert["kind"] == "credit_exhausted"
    assert alert["dashboard"] == API_BASE + "/engelbart"
    assert "sk-live-secret" not in json.dumps(alert)
    assert CA.credit_alert(traj, env, now=CA.CREDIT_CHECK_SECONDS * 3) is None
    # Topped back up and exhausted again: the event re-arms.
    monkeypatch.setattr(CH, "_request", answer("active"))
    assert CA.credit_alert(traj, env, now=CA.CREDIT_CHECK_SECONDS * 5) is None
    monkeypatch.setattr(CH, "_request", answer("exhausted"))
    assert CA.credit_alert(traj, env, now=CA.CREDIT_CHECK_SECONDS * 7)


def test_credit_alert_stays_quiet_without_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "prefix", str(tmp_path / "nowhere" / "venv"))
    monkeypatch.setattr(CH, "_request",
                        lambda record: (_ for _ in ()).throw(AssertionError(
                            "no credentials means no round trip")))
    assert CA.credit_alert(tmp_path / "traj", env_for(tmp_path), now=0) is None


def test_credit_alert_treats_an_unreachable_server_as_no_news(tmp_path, monkeypatch):
    write_credentials(tmp_path)
    sh_helper(tmp_path, tmp_path / "root" / "auth.json", settings_file(tmp_path))
    traj = tmp_path / "traj"
    env = env_for(tmp_path)
    monkeypatch.setattr(CH, "_request", lambda record: (0, None, "down"))
    assert CA.credit_alert(traj, env, now=0) is None
    # The failed check still counts as checked: no hammering a down server.
    record = json.loads((traj / CA.ALERT_FILE).read_text(encoding="utf-8"))
    assert record["checked_at"] == 0 and not record["alerted"]
