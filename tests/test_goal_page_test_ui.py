"""Alternate rendering, exercised through the installed frontend contracts.

Real loopback HTTP, goals/chat files, event capture and preview subprocess.
Only model replies and the external build executable are replaced in tests.
"""
import json
import re
import socket
import tempfile
from pathlib import Path
from unittest import mock

from test_goal_page import (ChatCase, BrowserCase, seed_design, server_for, fetch,
    get_json, post_json, bind_project, fake_chat, chat_answer, stored_rows, wait_for,
    GOAL_TITLE, SUBGOAL_TITLES, FIRST_TODOS, CS, GM, BUILD, CHAT_AGENT, AGENT_EVENTS, ui)


class AlternateRoutes(ChatCase):
    def test_routes_keep_production_and_query_selection(self):
        goal, subs = seed_design(self.chat)
        with server_for(self.chat) as url:
            self.assertIn(b'/goal/app.js', fetch(url + '/')[2])
            self.assertNotIn(b'/goal/test/', fetch(url + '/')[2])
            for path in ('/test', '/test/', '/test?goal=' + goal):
                status, headers, body = fetch(url + path)
                self.assertEqual(200, status)
                self.assertIn(b'/goal/test/app.js', body)
                self.assertIn('no-store', headers['Cache-Control'])
            status, headers, font = fetch(url + '/goal/test/source-code-pro.woff2')
            self.assertEqual((200, 'font/woff2', b'wOF2'), (status, headers['Content-Type'], font[:4]))
            answer = get_json(url + '/api/goal-page?goal=' + goal)
            self.assertEqual(subs[0], answer['subgoals'][0]['id'])

    def test_lifecycle_is_a_read_only_projection_with_preserved_row_scope(self):
        _, subs = seed_design(self.chat)
        for kind, payload, expected in (
            ('build.started', {'rows': ['r1']}, 'building'),
            ('verify.started', {'rows': ['r1']}, 'checking'),
            ('verify.failed', {'rows': ['r1'], 'reason': 'wrong control'}, 'failed'),
            ('build.repair_requested', {'rows': ['r1'], 'attempt': 1}, 'fixing'),
            ('build.started', {'rows': ['r1'], 'repair': 1}, 'fixing'),
            ('verify.started', {'rows': ['r1']}, 'checking'),
            ('verify.passed', {'rows': ['r1']}, 'done'),
            ('verify.escalated', {'reason': 'needs a decision'}, 'needs_user'),
        ):
            AGENT_EVENTS.record('chat', self.root, AGENT_EVENTS.new_event(
                kind, 'system', payload, subgoal_id=subs[0]))
            before = AGENT_EVENTS.path('chat', self.root).read_bytes()
            phase = ui._goal_page_panes(self.chat, True, subs[0])['build']['phase']
            self.assertEqual(expected, phase['status'])
            self.assertEqual(['r1'], phase['todoIds'])
            self.assertEqual(before, AGENT_EVENTS.path('chat', self.root).read_bytes())
        self.assertNotIn('phase', ui._goal_page_panes(self.chat, True, subs[1])['build'])
        AGENT_EVENTS.record('chat', self.root, AGENT_EVENTS.new_event(
            'todo.done_toggled', 'user', {'ok': True, 'done': True}, subgoal_id=subs[0], todo_id='r1'))
        self.assertNotIn('phase', ui._goal_page_panes(self.chat, True, subs[0])['build'])

    def test_stale_browser_save_preserves_other_turns_and_settled_proposals(self):
        _, subs = seed_design(self.chat)
        a = {'id': 'a', 'who': 'you', 'kind': 'text', 'text': 'First page'}
        b = {'id': 'b', 'who': 'you', 'kind': 'text', 'text': 'Second page'}
        proposal = {'id': 'p', 'who': 'bart', 'kind': 'proposal', 'text': 'Save it', 'added': True}
        with server_for(self.chat) as url:
            for messages in ([a, proposal], [b], [dict(proposal, added=False)]):
                answer = post_json(url + '/api/goal-page/chat', {'subgoal_id': subs[0], 'messages': messages})
                self.assertTrue(answer['ok'])
            saved = get_json(url + '/api/goal-page')['slices'][subs[0]]['chat']
            self.assertEqual(['a', 'p', 'b'], [m['id'] for m in saved])
            self.assertTrue(saved[1]['added'])


class AlternateBrowser(BrowserCase):
    def test_shared_todos_bart_build_and_plan_updates(self):
        goal, subs = seed_design(self.chat)
        project = bind_project(self)
        manifest = CS.paths('chat', self.root).manifest
        manifest.write_text(json.dumps({**json.loads(manifest.read_text()), 'origin': 'workspace'}))
        starts = []
        def start(session_id, root, goal_id, row_ids, quick=False):
            starts.append((session_id, goal_id, row_ids))
            goals, important = CS.load_goals(session_id, root)
            for row in GM.by_id(goals, goal_id)['todo_items']:
                if row['id'] in row_ids:
                    row['status'] = 'building'
            CS.save_goals(session_id, goals, important, root)
            BUILD.note_activity(session_id, root, goal_id, 'start', 'Building the actual selected rows')
            return {'ok': True, 'started': True, 'rows': row_ids}
        ask = fake_chat(chat_answer('This is the persisted reply.', ['Add a useful test']))
        with mock.patch.object(BUILD, 'start', start), mock.patch.object(CHAT_AGENT, 'ask', ask), \
             server_for(self.chat) as url, self.page_on(url + '/test?goal=' + goal) as (page, errors):
            expect = self.expect
            normal = page.context.browser.new_page()
            normal.goto(url + '/?goal=' + goal)
            expect(page.get_by_role('heading', name=GOAL_TITLE)).to_be_visible()
            self.assertEqual(project.name, page.evaluate('engelbart.store.get().project.name'))
            expect(page.locator('.rail .todo')).to_have_count(3)
            expect(page.get_by_label('Todo', exact=True).first).to_have_value(FIRST_TODOS[0])
            page.locator('.sub').nth(1).click()
            expect(page.locator('.sub').nth(1)).to_have_attribute('aria-current', 'true')
            expect(page.get_by_label('Todo', exact=True)).to_have_count(0)
            page.locator('.sub').first.click()
            new = page.get_by_label('New todo')
            new.fill('A shared todo')
            new.press('Enter')
            expect(normal.get_by_label('Todo', exact=True)).to_have_count(3)
            row = page.get_by_label('Todo', exact=True).last
            row.fill('A shared edited todo')
            expect(normal.get_by_label('Todo', exact=True).last).to_have_value('A shared edited todo')
            page.locator('.todo:not(.todo-new)').last.get_by_label('Remove todo').click()
            expect(normal.get_by_label('Todo', exact=True)).to_have_count(2)
            # Reverse direction, on the same files and event stream.
            normal.get_by_label('New todo').fill('From the normal page')
            normal.get_by_label('New todo').press('Enter')
            expect(page.get_by_label('Todo', exact=True).last).to_have_value('From the normal page')
            self.assertNotIn('overseer.routed', {e['type'] for e in AGENT_EVENTS.read('chat', self.root)})
            page.get_by_label('Message Bart').fill('Explain this project')
            page.get_by_label('Message Bart').press('Enter')
            expect(page.locator('.from-bart .bubble')).to_have_text('This is the persisted reply.')
            expect(normal.locator('.from-bart .bubble')).to_have_text('This is the persisted reply.', timeout=7000)
            page.get_by_role('button', name='Add', exact=True).click()
            expect(normal.locator('.proposal-note')).to_have_text('added to todos', timeout=7000)
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'engelbart-test-conversation.png'))
            page.reload()
            expect(page.locator('.from-bart .bubble')).to_have_text('This is the persisted reply.')
            expect(page.get_by_label('Todo', exact=True).last).to_have_value('Add a useful test')
            page.get_by_role('button', name='Build all').click()
            expect(page.locator('.todo-status').first).to_have_text('Building…')
            page.wait_for_function('window.engelbart.store.get().building === null')
            self.assertEqual(('chat', subs[0]), starts[0][:2])
            expect(normal.locator('.todo-status').first).to_have_text('Building…')
            page.get_by_role('tab', name='Terminal').click()
            expect(page.locator('.terminal')).to_contain_text('Building the actual selected rows')
            # External plan writer (the same goals.json Path updates).
            goals, important = self.goals()
            GM.by_id(goals, subs[1])['title'] = 'Revised project path'
            CS.save_goals('chat', goals, important, self.root)
            expect(page.locator('.sub').nth(1)).to_contain_text('Revised project path')
            expect(normal.locator('.sub').nth(1)).to_contain_text('Revised project path')
            events = AGENT_EVENTS.read('chat', self.root)
            types = {e['type'] for e in events}
            self.assertTrue({'project.opened', 'goal.opened', 'subgoal.selected', 'tab.changed',
                'todo.add_started', 'chat.saved', 'plan.suggestion_accepted'}.issubset(types), types)
            self.assertEqual([], errors)
            normal.close()

    def test_real_preview_terminal_lifecycle_and_responsive_layout(self):
        goal, subs = seed_design(self.chat)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        bind_project(self, serving_port=port)
        rows = [r['id'] for r in GM.by_id(self.goals()[0], subs[0])['todo_items']]
        with server_for(self.chat) as url, self.page_on(url + '/test') as (page, errors):
            expect = self.expect
            expect(page.get_by_label('New todo')).to_be_visible()
            for kind, payload, label in (
                ('build.started', {'rows': rows}, 'Building'),
                ('verify.started', {'rows': rows}, 'Checking'),
                ('build.repair_requested', {'rows': rows}, 'Fixing'),
                ('verify.started', {'rows': rows}, 'Checking'),
                ('verify.escalated', {'reason': 'Choose a direction'}, 'Needs user'),
                ('verify.failed', {'rows': rows}, 'Failed'),
            ):
                AGENT_EVENTS.record('chat', self.root, AGENT_EVENTS.new_event(kind, 'system', payload, subgoal_id=subs[0]))
                BUILD.note_activity('chat', self.root, subs[0], 'verify', label + ' activity')
                display = {'Building': 'Building…', 'Checking': 'Checking…', 'Fixing': 'Fixing…', 'Needs user': 'Needs you'}.get(label, label)
                expect(page.locator('.execution-status')).to_have_text(display, timeout=7000)
                expect(page.locator('.todo-status').first).to_have_text(display)
            goals, important = self.goals()
            for row in GM.by_id(goals, subs[0])['todo_items']:
                row.update(done=True, status='done')
            CS.save_goals('chat', goals, important, self.root)
            AGENT_EVENTS.record('chat', self.root, AGENT_EVENTS.new_event('verify.passed', 'system', {'rows': rows}, subgoal_id=subs[0]))
            expect(page.locator('.todo-status').first).to_have_text('Done', timeout=7000)
            page.get_by_role('tab', name='Terminal').click()
            for label in ('Building', 'Checking', 'Fixing', 'Needs user', 'Failed'):
                expect(page.locator('.terminal')).to_contain_text(label + ' activity')
            page.get_by_role('tab', name='Live preview').click()
            expect(page.get_by_role('button', name='Find how to run it')).to_be_visible()
            page.get_by_role('button', name='Find how to run it').click()
            page.get_by_role('button', name='Show UI', exact=True).click()
            frame = page.locator('.preview-frame')
            expect(frame).to_be_visible(timeout=20000)
            expect(frame.content_frame.get_by_role('heading', name='the app')).to_be_visible()
            frame.content_frame.get_by_role('heading', name='the app').click()
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'engelbart-test-preview.png'))
            page.get_by_role('button', name='Stop', exact=True).click()
            expect(page.locator('.preview')).to_contain_text('It ended', timeout=10000)
            page.get_by_role('tab', name='Bart', exact=True).click()
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'engelbart-test-desktop.png'))
            page.set_viewport_size({'width': 390, 'height': 844})
            expect(page.get_by_label('Message Bart')).to_be_visible()
            self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
            page.screenshot(path=str(Path(tempfile.gettempdir()) / 'engelbart-test-mobile.png'))
            types = {e['type'] for e in AGENT_EVENTS.read('chat', self.root)}
            self.assertTrue({'preview.opened', 'preview.closed', 'preview.interacted', 'artifact.opened'}.issubset(types))
            self.assertEqual([], errors)

class AlternateAccount(BrowserCase):
    def page_on(self, url):
        return super().page_on(url + '/test')

    def test_existing_account_and_expertise_flow(self):
        import test_goal_page
        test_goal_page.GoalPageBrowserTests.test_the_account_icon_says_who_the_machine_is_connected_as(self)

    def test_query_and_header_navigation_stay_on_test(self):
        first, _ = seed_design(self.chat)
        second = ui._apply({'op': 'add_goal', 'title': 'Second real goal'}, self.chat)['id']
        ui._apply({'op': 'add_goal', 'title': 'Second real subgoal', 'parent_goal_id': second}, self.chat)
        with server_for(self.chat) as url, super().page_on(url + '/test?goal=' + second) as (page, errors):
            self.expect(page.locator('.goal-title')).to_have_text('Second real goal')
            self.expect(page.locator('.sub-title')).to_have_text('Second real subgoal')
            page.evaluate('(id) => engelbart.actions.openGoal(id)', first)
            self.assertIn('/test?goal=' + first, page.url)
            self.expect(page.locator('.goal-title')).to_have_text(GOAL_TITLE)
            page.reload()
            self.expect(page.locator('.goal-title')).to_have_text(GOAL_TITLE)
            self.assertEqual([], errors)
