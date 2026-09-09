"""The rail reference edits a separate demo store and simulates builds only."""
import importlib.util
import http.client
from http.server import ThreadingHTTPServer
import tempfile
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LegacyRailPreviewTests(unittest.TestCase):
    def setUp(self):
        script = ROOT / 'tools/legacy_rail_preview/serve.py'
        self.assertTrue(script.is_file(), 'The isolated rail preview server exists')
        spec = importlib.util.spec_from_file_location('rail_preview', script)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = self.module.DemoStore(Path(self.tmp.name) / 'demo.json')

    def test_edits_survive_reload_and_old_revisions_are_refused(self):
        state = self.store.snapshot()
        goal = state['goals'][0]
        goal['todo_items'][0]['text'] = 'Edited in the reference'
        result = self.store.import_tree({'goals': [goal], 'base_revision': state['revision']})
        self.assertTrue(result['ok'])
        restored = self.module.DemoStore(self.store.path).snapshot()
        self.assertEqual('Edited in the reference', restored['goals'][0]['todo_items'][0]['text'])
        self.assertFalse(self.store.import_tree({'goals': [], 'base_revision': state['revision']})['ok'])

    def test_simulation_builds_only_selected_rows(self):
        state = self.store.snapshot()
        goal = state['goals'][0]
        row = goal['todo_items'][0]
        result = self.store.op({'op': 'build_todos', 'goal_id': goal['id'], 'ids': [row['id']]})
        self.assertTrue(result['simulated'])
        self.assertEqual('building', self.store.snapshot()['goals'][0]['todo_items'][0]['status'])
        self.store.finish_builds(float('inf'))
        rows = self.store.snapshot()['goals'][0]['todo_items']
        self.assertEqual('done', rows[0]['status'])
        self.assertEqual('', rows[1]['status'])

    def test_unknown_operations_do_not_mutate_the_store(self):
        before = self.store.snapshot()
        self.assertFalse(self.store.op({'op': 'launch_agent_run'})['ok'])
        self.assertEqual(before, self.store.snapshot())

    def test_cancelled_simulation_does_not_finish_later(self):
        goal = self.store.snapshot()['goals'][0]
        ids = [goal['todo_items'][0]['id']]
        self.store.op({'op': 'build_todos', 'goal_id': goal['id'], 'ids': ids})
        answer = self.store.op({'op': 'cancel_todos', 'goal_id': goal['id'], 'ids': ids})
        self.assertTrue(answer['ok'])
        self.store.finish_builds(float('inf'))
        self.assertEqual('', self.store.snapshot()['goals'][0]['todo_items'][0]['status'])

    def test_understanding_saves_without_calling_an_agent(self):
        goal = self.store.snapshot()['goals'][0]
        result = self.store.op({'op': 'set_understanding', 'goal_id': goal['id'],
                               'scenario': 'Drafting a plan', 'questions': [], 'shots': []})
        self.assertTrue(result['ok'])
        self.assertEqual('Drafting a plan', self.store.snapshot()['goals'][0]['understanding']['scenario'])

    def test_foreign_hosts_cannot_read_or_edit_the_demo(self):
        server = ThreadingHTTPServer(('127.0.0.1', 0), self.module.Handler)
        server.store = self.store
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for method, route in [('GET', '/api/state'), ('POST', '/api/op')]:
                conn = http.client.HTTPConnection('127.0.0.1', server.server_port)
                try:
                    conn.request(method, route, headers={'Host': 'foreign.example'})
                    response = conn.getresponse()
                    self.assertEqual(403, response.status)
                    response.read()
                finally:
                    conn.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
