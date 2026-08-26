"""Signing in through the browser the reader is already signed into.

A password typed at a terminal is a password the terminal has seen. The
ordinary way in is a provider button in a browser, and the CLI is handed
the result -- so the terminal never learns a secret it has no use for.

PKCE rather than the implicit flow for one practical reason: implicit
returns tokens in the URL *fragment*, which a browser never sends to a
server, so a local listener could not see them without a page that reads
location.hash and posts it back. PKCE returns a code in the query string,
and a code is worthless without the verifier this process kept.
"""
import base64
import hashlib
import json
import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "hc" / "src"))

from human_compact.trajectory import supabase_client as SB  # noqa: E402


class Configured(unittest.TestCase):
    """A client pointed at a project, with nothing signed in."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "supabase.json").write_text(json.dumps(
            {"url": "https://ref.supabase.co", "anon_key": "eyJanon"}))
        for name, path in (("config_path", self.root / "supabase.json"),
                           ("session_path", self.root / "session.json")):
            patch = mock.patch.object(SB, name, lambda r=None, p=path: p)
            patch.start()
            self.addCleanup(patch.stop)
        self.posted = []

        def post(url, headers, body, where="rpc"):
            self.posted.append({"url": url, "headers": headers, "body": body})
            return {"access_token": "at", "refresh_token": "rt",
                    "expires_in": 3600,
                    "user": {"id": "u1", "email": "someone@example.com"}}

        patch = mock.patch.object(SB, "_post", post)
        patch.start()
        self.addCleanup(patch.stop)


class PkceTests(unittest.TestCase):

    def test_the_challenge_is_the_sha256_of_the_verifier(self):
        pair = SB.pkce_pair()
        want = base64.urlsafe_b64encode(
            hashlib.sha256(pair["verifier"].encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
        self.assertEqual(want, pair["challenge"])

    def test_both_halves_are_url_safe_and_unpadded(self):
        # They travel in a query string; padding and "+" do not.
        pair = SB.pkce_pair()
        for half in pair.values():
            self.assertNotIn("=", half)
            self.assertTrue(all(c.isalnum() or c in "-_" for c in half), half)

    def test_a_fresh_pair_every_time(self):
        self.assertNotEqual(SB.pkce_pair()["verifier"],
                            SB.pkce_pair()["verifier"])

    def test_the_verifier_is_long_enough_to_be_worth_keeping(self):
        # RFC 7636 puts the floor at 43 characters.
        self.assertGreaterEqual(len(SB.pkce_pair()["verifier"]), 43)


class AuthorizeUrlTests(Configured):

    def test_it_names_the_provider_the_redirect_and_the_challenge(self):
        url = SB.authorize_url("google", "http://127.0.0.1:5050/callback", "CH")
        parts = urllib.parse.urlsplit(url)
        query = urllib.parse.parse_qs(parts.query)
        self.assertEqual("https://ref.supabase.co/auth/v1/authorize",
                         parts.scheme + "://" + parts.netloc + parts.path)
        self.assertEqual(["google"], query["provider"])
        self.assertEqual(["http://127.0.0.1:5050/callback"], query["redirect_to"])
        self.assertEqual(["CH"], query["code_challenge"])
        self.assertEqual(["s256"], query["code_challenge_method"])

    def test_github_is_offered_too(self):
        self.assertIn("provider=github",
                      SB.authorize_url("github", "http://127.0.0.1:1/cb", "C"))

    def test_a_provider_nobody_configured_is_refused_here(self):
        # Better than sending the reader to a page that will not load.
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.authorize_url("myspace", "http://127.0.0.1:1/cb", "C")
        self.assertIn("unknown sign-in provider", str(caught.exception))

    def test_with_no_project_configured_it_says_so(self):
        (self.root / "supabase.json").write_text("{}")
        with self.assertRaises(SB.SupabaseError):
            SB.authorize_url("google", "http://127.0.0.1:1/cb", "C")


class ExchangeTests(Configured):

    def test_the_code_and_verifier_buy_a_stored_session(self):
        out = SB.exchange_code("THECODE", "THEVERIFIER")
        sent = self.posted[0]
        self.assertEqual("https://ref.supabase.co/auth/v1/token?grant_type=pkce",
                         sent["url"])
        self.assertEqual({"auth_code": "THECODE",
                          "code_verifier": "THEVERIFIER"}, sent["body"])
        self.assertEqual("eyJanon", sent["headers"]["apikey"])
        self.assertEqual("someone@example.com", out["email"])

    def test_it_lands_where_a_password_sign_in_would_have(self):
        # The rest of the client must not know which way the reader came in.
        SB.exchange_code("THECODE", "THEVERIFIER")
        kept = json.loads((self.root / "session.json").read_text())
        self.assertEqual("at", kept["access_token"])
        self.assertEqual("rt", kept["refresh_token"])
        self.assertEqual("u1", kept["user_id"])

    def test_no_code_is_refused_without_asking_supabase(self):
        with self.assertRaises(SB.SupabaseError):
            SB.exchange_code("", "THEVERIFIER")
        self.assertEqual([], self.posted)


class RoundTripTests(Configured):
    """The listener, the browser and the exchange, together."""

    def browser(self, query="?code=THECODE", status=None):
        """Stand in for Supabase redirecting back to the listener."""
        seen = {}

        def open_it(url):
            seen["url"] = url
            back = urllib.parse.parse_qs(
                urllib.parse.urlsplit(url).query)["redirect_to"][0]
            try:
                with urllib.request.urlopen(back + query, timeout=5) as r:
                    seen["status"] = r.status
                    seen["page"] = r.read().decode()
            except urllib.error.HTTPError as exc:
                seen["status"] = exc.code
                seen["page"] = exc.read().decode()
        return open_it, seen

    def test_a_signed_in_browser_leaves_the_terminal_signed_in(self):
        open_it, seen = self.browser()
        out = SB.sign_in_with_browser("google", open_browser=open_it, wait_s=10)
        self.assertEqual("someone@example.com", out["email"])
        self.assertEqual({"auth_code": "THECODE",
                          "code_verifier": mock.ANY}, self.posted[0]["body"])
        # The verifier that was exchanged is the one whose challenge went out.
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(seen["url"]).query)
        want = base64.urlsafe_b64encode(hashlib.sha256(
            self.posted[0]["body"]["code_verifier"].encode()).digest()
        ).decode().rstrip("=")
        self.assertEqual([want], query["code_challenge"])

    def test_the_listener_is_on_loopback_and_a_port_nobody_reserved(self):
        open_it, seen = self.browser()
        SB.sign_in_with_browser("google", open_browser=open_it, wait_s=10)
        back = urllib.parse.parse_qs(
            urllib.parse.urlsplit(seen["url"]).query)["redirect_to"][0]
        self.assertTrue(back.startswith("http://127.0.0.1:"), back)
        self.assertTrue(back.endswith("/callback"), back)

    def test_the_browser_is_told_it_worked(self):
        open_it, seen = self.browser()
        SB.sign_in_with_browser("google", open_browser=open_it, wait_s=10)
        self.assertEqual(200, seen["status"])
        self.assertIn("Signed in", seen["page"])
        self.assertIn("close this tab", seen["page"])

    def test_a_reader_who_cancels_is_told_why_rather_than_waited_on(self):
        open_it, seen = self.browser("?error=access_denied"
                                     "&error_description=User+said+no")
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.sign_in_with_browser("google", open_browser=open_it, wait_s=10)
        self.assertIn("User said no", str(caught.exception))
        self.assertEqual(400, seen["status"])
        self.assertEqual([], self.posted)

    def test_a_browser_that_never_comes_back_gives_up(self):
        # A reader who wanders off is not a reader to wait on for ever.
        with self.assertRaises(SB.SupabaseError) as caught:
            SB.sign_in_with_browser("google", open_browser=lambda url: None,
                                    wait_s=1)
        self.assertIn("timed out", str(caught.exception))
        self.assertEqual([], self.posted)

    def test_the_reader_is_shown_the_url_in_case_it_did_not_open(self):
        said = []
        open_it, _seen = self.browser()
        SB.sign_in_with_browser("google", open_browser=open_it, wait_s=10,
                                announce=said.append)
        self.assertEqual(1, len(said))
        self.assertIn("/auth/v1/authorize", said[0])

    def test_an_unknown_provider_never_opens_a_listener(self):
        opened = []
        with self.assertRaises(SB.SupabaseError):
            SB.sign_in_with_browser("myspace", open_browser=opened.append,
                                    wait_s=1)
        self.assertEqual([], opened)


if __name__ == "__main__":
    unittest.main()
