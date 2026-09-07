"""Acceptance surface is production /, using real project and event stores."""
import json
from test_project_resources import resource, pdf_bytes, bind_project
from test_goal_page import BrowserCase, seed_design, server_for, CS, GM
from human_compact.trajectory import resources as R, build as BUILD
from human_compact.trajectory.agents import events as EV


class ProductionWorkspaceTests(BrowserCase):
    def prepare(self, kinds):
        _, subs = seed_design(self.chat)
        cwd = bind_project(self)
        manifest = CS.load_manifest('chat', self.root)
        manifest['project_home'] = str(cwd)
        CS.paths('chat', self.root).manifest.write_text(json.dumps(manifest))
        records = [resource(kind) for kind in kinds]
        if 'dataset' in kinds:
            records[-1]['provenance'] = {'fallbackOf': {'kind': 'synthetic_fallback', 'title': 'IDETrace Dataset', 'reason': 'Original requires access approval'}}
        def fetch(url, path, limit):
            path.write_bytes(pdf_bytes() if path.suffix == '.pdf' else b'student_id,action\ndemo-1,<script>alert(1)</script>\ndemo-2,edit\n')
        R.prepare(self.root, cwd, records, fetch=fetch)
        return subs

    def test_conditional_tabs_in_exact_order_and_no_resource_rail(self):
        self.prepare(['paper', 'dataset'])
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            self.expect(page.get_by_role('tab')).to_have_text(['Bart', 'Live preview', 'Terminal', 'Paper', 'Dataset'])
            self.expect(page.get_by_label('Plan').get_by_text('Resources', exact=True)).to_have_count(0)
            page.get_by_role('tab', name='Dataset', exact=True).click()
            details = page.get_by_label('Resource details')
            self.expect(details).to_contain_text('Synthetic stand-in for IDETrace Dataset')
            self.expect(details).to_contain_text('Original requires access approval')
            self.expect(details.get_by_role('table')).to_contain_text('<script>alert(1)</script>')
            self.expect(details.locator('script')).to_have_count(0)
            self.assertLessEqual(details.locator('tbody tr').count(), 10)
            self.expect(details).to_contain_text('student_id')
            page.get_by_role('tab', name='Paper', exact=True).click()
            self.expect(page.locator('iframe.paper-frame')).to_have_attribute('src', '/api/project-paper?id=paper-one')
            page.reload()
            self.expect(page.get_by_role('tab')).to_have_text(['Bart', 'Live preview', 'Terminal', 'Paper', 'Dataset'])
            self.assertEqual([], errors)

    def test_no_resources_has_only_normal_tabs(self):
        seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            self.expect(page.get_by_role('tab')).to_have_text(['Bart', 'Live preview', 'Terminal'])

    def test_paper_only(self):
        self.prepare(['paper'])
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            self.expect(page.get_by_role('tab')).to_have_text(['Bart', 'Live preview', 'Terminal', 'Paper'])

    def test_dataset_only(self):
        self.prepare(['dataset'])
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            self.expect(page.get_by_role('tab')).to_have_text(['Bart', 'Live preview', 'Terminal', 'Dataset'])

    def test_live_factual_activity_and_lifecycle_outside_terminal(self):
        subs = self.prepare([])
        goals, important = self.goals()
        rows = GM.by_id(goals, subs[0])['todo_items']
        ids = [r['id'] for r in rows]
        def emit(kind):
            EV.record('chat', self.root, EV.new_event(kind, 'system', {'rows': ids}, subgoal_id=subs[0]))
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            emit('build.started')
            BUILD.note_activity('chat', self.root, subs[0], 'tool', 'edited index.html')
            self.expect(page.get_by_label('Build activity')).to_contain_text('edited index.html')
            BUILD.note_activity('chat', self.root, subs[0], 'tool', 'edited server.js')
            self.expect(page.get_by_label('Build activity')).to_contain_text('edited server.js')
            for kind, label in [('verify.started', 'Checking'), ('build.repair_requested', 'Fixing'), ('verify.started', 'Checking')]:
                emit(kind)
                self.expect(page.get_by_label('Build activity')).to_contain_text(label)
            for r in rows: r.update(status='done', done=True)
            CS.save_goals('chat', goals, important, self.root)
            emit('verify.passed')
            self.expect(page.get_by_label('Build activity')).to_have_count(0)
            page.get_by_role('tab', name='Terminal', exact=True).click()
            self.expect(page.get_by_role('tabpanel')).to_contain_text('edited server.js')
            self.assertEqual([], errors)

    def test_standalone_failed_preview_is_diagnosed_without_running_model_shell(self):
        from unittest import mock
        from human_compact.trajectory import preview
        self.prepare([])
        state = {'ok': True, 'status': 'failed', 'run': {'command': 'npm run dev', 'exit_code': 1, 'lines': ['API_KEY is missing']}}
        def recover(*args, **kwargs):
            state['recovery'] = {'ok': False, 'status': 'needs_user', 'reason': 'This project requires an API key.'}
            return state['recovery']
        with mock.patch.object(preview, 'state', side_effect=lambda *a, **k: state), mock.patch.object(preview, 'running', return_value=mock.Mock()), mock.patch.object(preview, 'recover', side_effect=recover) as diagnosis, server_for(self.chat) as url, self.page_on(url) as (page, errors):
            page.get_by_role('tab', name='Live preview', exact=True).click()
            self.expect(page.get_by_role('tabpanel')).to_contain_text('Needs you')
            self.expect(page.get_by_role('tabpanel')).to_contain_text('requires an API key')
            diagnosis.assert_called_once()
            page.get_by_role('tab', name='Terminal', exact=True).click()
            self.expect(page.get_by_role('tabpanel')).to_contain_text('API_KEY is missing')
