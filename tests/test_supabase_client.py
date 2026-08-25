"""Where the keys live, and what the button says before it is pressed.

Nothing here reaches the network: the transport is stubbed, because what
matters is which file is read, what is never written to it, and whether a
failure comes back as a sentence the reader can act on.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import supabase_client as SB  # noqa: E402


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.vault = Path(self.tmp.name) / "vault"
        (self.vault / "chat-sessions").mkdir(parents=True)
        self.env = mock.patch.dict(os.environ, {
            "CLAUDE_VAULT_DIR": str(self.vault)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for name in ("HC_SUPABASE_URL", "HC_SUPABASE_ANON_KEY"):
            os.environ.pop(name, None)

    def write(self, **fields):
        SB.config_path().parent.mkdir(parents=True, exist_ok=True)
        SB.config_path().write_text(json.dumps(dict(
            {"url": "https://ref.supabase.co", "anon_key": "anon-key"},
            **fields)))


class LocationTests(VaultTests):
    def test_the_keys_live_in_the_vault_not_in_any_repository(self):
        self.assertEqual(self.vault / "supabase.json", SB.config_path())
        self.assertEqual(self.vault / "supabase-session.json",
                         SB.session_path())

    def test_a_server_and_a_cli_land_on_the_same_file(self):
        # The server derives its root from a session directory, so it holds
        # <vault>/chat-sessions; a CLI passes nothing. One file either way,
        # or the button reads what the setup command never wrote.
        self.assertEqual(SB.config_path(),
                         SB.config_path(self.vault / "chat-sessions"))

    def test_the_template_is_written_private_and_never_overwrites_keys(self):
        path, created = SB.write_template()
        self.assertTrue(created)
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertNotIn("configured", json.loads(path.read_text()))
        path.write_text(json.dumps({"url": "https://real.supabase.co",
                                    "anon_key": "real-key"}))
        again, created_again = SB.write_template()
        self.assertFalse(created_again)
        self.assertEqual("real-key", json.loads(again.read_text())["anon_key"])


class ConfigTests(VaultTests):
    def test_a_template_left_unfilled_reads_as_unconfigured(self):
        SB.write_template()
        self.assertFalse(SB.configured())
        self.assertFalse(SB.status()["configured"])

    def test_a_filled_file_is_configured(self):
        self.write()
        self.assertTrue(SB.configured())

    def test_the_environment_wins_over_the_file(self):
        self.write(url="https://file.supabase.co")
        with mock.patch.dict(os.environ, {
                "HC_SUPABASE_URL": "https://env.supabase.co"}):
            self.assertEqual("https://env.supabase.co", SB.load_config()["url"])

    def test_a_key_is_never_sent_in_the_clear(self):
        self.write(url="http://ref.supabase.co")
        with self.assertRaises(SB.SupabaseError):
            SB.load_config()

    def test_a_local_stack_may_be_plain_http(self):
        self.write(url="http://127.0.0.1:54321")
        self.assertEqual("http://127.0.0.1:54321", SB.load_config()["url"])


class SaveConfigTests(VaultTests):
    def test_a_pasted_rest_endpoint_is_saved_as_the_origin(self):
        path = SB.save_config("https://ref.supabase.co/rest/v1", "anon-key")
        self.assertEqual("https://ref.supabase.co",
                         json.loads(path.read_text())["url"])
        self.assertEqual("https://ref.supabase.co", SB.load_config()["url"])

    def test_the_panel_can_write_the_url_and_the_anon_key(self):
        path = SB.save_config("https://ref.supabase.co/", "anon-key",
                              "me@example.com")
        stored = json.loads(path.read_text())
        self.assertEqual("https://ref.supabase.co", stored["url"])
        self.assertEqual("anon-key", stored["anon_key"])
        self.assertEqual(0o600, path.stat().st_mode & 0o777)
        self.assertTrue(SB.configured())

    def test_a_service_key_is_refused_with_a_reason(self):
        import base64
        body = base64.urlsafe_b64encode(
            json.dumps({"role": "service_role"}).encode()).decode().rstrip("=")
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.save_config("https://ref.supabase.co", "h." + body + ".s")
        self.assertIn("anon", str(caught.exception))

    def test_an_anon_key_of_the_same_shape_is_kept(self):
        import base64
        body = base64.urlsafe_b64encode(
            json.dumps({"role": "anon"}).encode()).decode().rstrip("=")
        SB.save_config("https://ref.supabase.co", "h." + body + ".s")
        self.assertTrue(SB.configured())

    def test_plain_http_is_refused_unless_it_is_local(self):
        with self.assertRaises(SB.SupabaseError):
            SB.save_config("http://ref.supabase.co", "anon-key")
        SB.save_config("http://localhost:54321", "anon-key")
        self.assertTrue(SB.configured())

    def test_writing_the_keys_leaves_other_fields_alone(self):
        SB.config_path().parent.mkdir(parents=True, exist_ok=True)
        SB.config_path().write_text(json.dumps({"note": "keep me"}))
        SB.save_config("https://ref.supabase.co", "anon-key")
        self.assertEqual("keep me", json.loads(
            SB.config_path().read_text())["note"])


class UrlTests(unittest.TestCase):
    """The dashboard shows several URLs; only the origin is wanted."""

    def test_an_api_endpoint_is_trimmed_back_to_the_origin(self):
        for pasted in ("https://ref.supabase.co/rest/v1",
                       "https://ref.supabase.co/rest/v1/",
                       "https://ref.supabase.co/auth/v1",
                       "https://ref.supabase.co/storage/v1",
                       "https://ref.supabase.co/functions/v1"):
            self.assertEqual("https://ref.supabase.co",
                             SB.normalize_url(pasted), pasted)

    def test_the_origin_itself_is_left_alone(self):
        self.assertEqual("https://ref.supabase.co",
                         SB.normalize_url("https://ref.supabase.co/"))

    def test_a_dashboard_link_becomes_the_project_it_names(self):
        self.assertEqual("https://abc123.supabase.co", SB.normalize_url(
            "https://supabase.com/dashboard/project/abc123"))
        self.assertEqual("https://abc123.supabase.co", SB.normalize_url(
            "https://supabase.com/dashboard/project/abc123/editor"))

    def test_nothing_is_nothing(self):
        self.assertEqual("", SB.normalize_url(""))
        self.assertEqual("", SB.normalize_url(None))

    def test_a_404_signing_in_is_not_blamed_on_the_migration(self):
        # The two 404s have nothing to do with each other, and one message
        # for both sends the reader to the wrong end of the problem.
        auth = SB._explain(404, "", "auth")
        self.assertIn("project URL", auth)
        self.assertNotIn("migration", auth)
        self.assertIn("migration", SB._explain(404, "", "rpc"))


class SessionTests(VaultTests):
    def test_signing_in_keeps_the_tokens_and_not_the_password(self):
        self.write()
        seen = {}

        def fake(url, headers, body, where="rpc"):
            seen["url"], seen["body"], seen["where"] = url, body, where
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600,
                    "user": {"id": "u-1", "email": "me@example.com"}}

        with mock.patch.object(SB, "_post", fake):
            session = SB.sign_in("me@example.com", "hunter2")
        self.assertEqual("u-1", session["user_id"])
        self.assertIn("grant_type=password", seen["url"])
        # Named as an auth call, so a 404 here is explained as a URL problem
        # rather than as a missing migration.
        self.assertEqual("auth", seen["where"])
        stored = SB.session_path().read_text()
        self.assertNotIn("hunter2", stored)
        self.assertIn("rt", stored)
        self.assertEqual(0o600, SB.session_path().stat().st_mode & 0o777)

    def test_a_lapsed_token_is_renewed_before_it_is_used(self):
        self.write()
        SB.session_path().write_text(json.dumps({
            "access_token": "old", "refresh_token": "rt",
            "expires_at": int(time.time()) - 5, "user_id": "u-1",
            "email": "me@example.com"}))
        calls = []

        def fake(url, headers, body, where="rpc"):
            calls.append(url)
            return {"access_token": "fresh", "refresh_token": "rt2",
                    "expires_in": 3600,
                    "user": {"id": "u-1", "email": "me@example.com"}}

        with mock.patch.object(SB, "_post", fake):
            session = SB.current_session()
        self.assertEqual("fresh", session["access_token"])
        self.assertTrue(any("refresh_token" in c for c in calls))

    def test_without_a_session_the_reader_is_told_what_to_run(self):
        self.write()
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.current_session()
        self.assertIn("supabase-login", str(caught.exception))

    def test_signing_out_removes_the_tokens(self):
        self.write()
        SB.session_path().write_text(json.dumps({"access_token": "at"}))
        SB.sign_out()
        self.assertFalse(SB.session_path().exists())


class MessageTests(VaultTests):
    def test_a_missing_function_names_the_migration(self):
        self.assertIn("migration", SB._explain(404, ""))

    def test_row_security_refusing_is_said_plainly(self):
        self.assertIn("row security", SB._explain(403, ""))

    def test_bad_credentials_are_said_plainly(self):
        self.assertIn("email and password",
                      SB._explain(400, "invalid grant", "auth"))

    def test_a_refused_call_repeats_what_postgres_said(self):
        # A 400 from a call is Postgres saying no, and its message is the
        # only useful part. Guessing "your credentials are wrong" here sent
        # the reader to the wrong end of the problem twice.
        body = json.dumps({"code": "42702",
                           "message": 'column reference "project_id" is ambiguous',
                           "hint": None})
        said = SB._explain(400, body, "rpc")
        self.assertIn("ambiguous", said)
        self.assertNotIn("password", said)

    def test_a_missing_function_is_named(self):
        body = json.dumps({"code": "PGRST202",
                           "message": "Could not find the function "
                                      "public.hc_add_member(p_email)"})
        said = SB._explain(404, body, "rpc")
        self.assertIn("hc_add_member", said)
        # Not blamed on whichever migration happens to be first to mind.
        self.assertNotIn("hc_sync_project", said)


class InviteCodeTests(unittest.TestCase):
    """One pasteable string carries where, which key, and which token."""

    def test_a_code_round_trips(self):
        code = SB.join_code("https://ref.supabase.co", "anon-key", "hcs_abc")
        self.assertTrue(code.startswith(SB.CODE_PREFIX))
        self.assertEqual({"url": "https://ref.supabase.co",
                          "anon_key": "anon-key", "token": "hcs_abc"},
                         SB.parse_code(code))

    def test_a_code_carries_no_padding_to_be_mangled(self):
        for token in ("hcs_a", "hcs_ab", "hcs_abc", "hcs_abcd"):
            code = SB.join_code("https://r.supabase.co", "k", token)
            self.assertNotIn("=", code)
            self.assertEqual(token, SB.parse_code(code)["token"])

    def test_something_that_is_not_a_code_says_so(self):
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.parse_code("just some text")
        self.assertIn(SB.CODE_PREFIX, str(caught.exception))

    def test_a_damaged_code_is_refused(self):
        with self.assertRaises(SB.SupabaseError):
            SB.parse_code(SB.CODE_PREFIX + "!!!not-base64!!!")

    def test_a_code_missing_a_part_is_refused(self):
        import base64
        half = base64.urlsafe_b64encode(b'{"u":"https://r.co"}').decode().rstrip("=")
        with self.assertRaises(SB.SupabaseError):
            SB.parse_code(SB.CODE_PREFIX + half)

    def test_a_pasted_endpoint_inside_a_code_is_normalized(self):
        code = SB.join_code("https://ref.supabase.co/rest/v1", "k", "hcs_x")
        self.assertEqual("https://ref.supabase.co", SB.parse_code(code)["url"])

    def test_a_closed_share_is_reported_in_the_words_the_server_used(self):
        code = SB.join_code("https://ref.supabase.co", "k", "hcs_x")
        with mock.patch.object(SB, "_post", lambda *a, **k: {
                "ok": False, "error": "this share is not open"}):
            with self.assertRaises(SB.SupabaseError) as caught:
                SB.read_shared(code)
        self.assertIn("not open", str(caught.exception))

    def test_reading_a_share_uses_only_the_anon_key(self):
        code = SB.join_code("https://ref.supabase.co", "anon-k", "hcs_x")
        seen = {}

        def fake(url, headers, body, where="rpc"):
            seen.update(url=url, headers=headers, body=body)
            return {"ok": True, "project": {}, "goals": []}

        with mock.patch.object(SB, "_post", fake):
            SB.read_shared(code)
        self.assertIn("hc_read_shared", seen["url"])
        # No session, no vault, no user token: the reader has no account.
        self.assertEqual("Bearer anon-k", seen["headers"]["Authorization"])
        self.assertEqual({"token": "hcs_x"}, seen["body"])


class SendTests(VaultTests):
    def test_a_project_directory_is_required(self):
        self.write()
        with self.assertRaises(SB.SupabaseError):
            SB.sync_project(None, "")

    def test_an_unfilled_config_names_the_file_to_fill(self):
        SB.write_template()
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.sync_project(None, "/tmp/whatever")
        self.assertIn("supabase.json", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
