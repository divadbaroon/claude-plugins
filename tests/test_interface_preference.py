"""Interface choice survives ports/restarts; both views reach shared settings."""
import time
from pathlib import Path
from unittest import mock

from test_goal_page import ChatCase, fetch, post_json, server_for
from human_compact import cli


class InterfacePreferenceTests(ChatCase):
    def test_choice_survives_another_server_and_does_not_change_goals(self):
        before = self.goals()
        with server_for(self.chat) as url:
            self.assertIn(b'/goal/app.js', fetch(url + '/')[2])
            result = post_json(url + '/api/interface', {'interface': 'legacy'})
            self.assertEqual({'ok': True, 'interface': 'legacy', 'url': '/legacy'}, result)
            self.assertIn(b'/bridge.js', fetch(url + '/')[2])
            self.assertIn(b'/goal/app.js', fetch(url + '/workspace?goal=g1')[2])
            # Explicit routes and settings reads do not silently change preference.
            self.assertIn(b'/goal/settings-page.js', fetch(url + '/settings')[2])
            self.assertIn(b'/bridge.js', fetch(url + '/')[2])
        other = self.root / 'another-chat'
        other.mkdir()
        with server_for(other) as url:
            self.assertIn(b'/bridge.js', fetch(url + '/')[2])
            post_json(url + '/api/interface', {'interface': 'goal'})
            self.assertIn(b'/goal/app.js', fetch(url + '/')[2])
            self.assertIn(b'/bridge.js', fetch(url + '/legacy')[2])
        self.assertEqual(before, self.goals())

    def test_invalid_choice_does_not_replace_saved_preference(self):
        with server_for(self.chat) as url:
            post_json(url + '/api/interface', {'interface': 'legacy'})
            for value in ['missing', '../../settings', None, [], {}]:
                import urllib.error
                with self.assertRaises(urllib.error.HTTPError) as error:
                    post_json(url + '/api/interface', {'interface': value})
                self.assertEqual(400, error.exception.code)
                error.exception.close()
            self.assertIn(b'/bridge.js', fetch(url + '/')[2])

    def test_foreign_origin_cannot_switch_interface(self):
        with server_for(self.chat) as url:
            import urllib.error
            with self.assertRaises(urllib.error.HTTPError) as error:
                post_json(url + '/api/interface', {'interface': 'legacy'},
                          {'Origin': 'https://example.com'})
            self.assertEqual(403, error.exception.code)
            error.exception.close()
            self.assertIn(b'/goal/app.js', fetch(url + '/')[2])


class RuntimeIdentityTests(ChatCase):
    def test_newer_timestamp_does_not_make_another_installation_current(self):
        record = {'started_at': time.time() + 3600,
                  'package_path': '/old/install/human_compact'}
        self.assertTrue(cli._server_outran_its_code(record))

    def test_current_installation_is_reusable(self):
        record = {'started_at': time.time() + 3600,
                  'package_path': str(Path(cli.__file__).resolve().parent)}
        self.assertFalse(cli._server_outran_its_code(record))

    def test_old_registry_is_checked_against_live_server_identity(self):
        with server_for(self.chat) as url:
            record = {'started_at': time.time() + 3600, 'url': url + '/'}
            with mock.patch.object(cli, '_package_path', return_value='/new/install'):
                self.assertTrue(cli._server_outran_its_code(record))
