"""Minimal lifecycle communication through real state, APIs, and DOM."""
from unittest import mock

from test_agents import AgentCase, Recorder, ROWS, PIECE, GOAL, _answer, mark_rows
from test_goal_page import BrowserCase, seed_design, server_for, ui, CS, GM, BUILD
from human_compact.trajectory.agents import events as EV, communication as COMM
from human_compact.trajectory.agents import orchestrator as ORCH


class CommunicationTests(AgentCase):
    def messages(self):
        return CS.load_bart_chats(self.session, self.root).get(PIECE, [])

    def phase(self):
        return ui._goal_page_build_phase(self.session, self.root, PIECE)

    def question_row(self):
        goals, important = CS.load_goals(self.session, self.root)
        row = GM.by_id(goals, PIECE)['todo_items'][0]
        row.update(status='asking', question='Use time since edit or successful run?')
        CS.save_goals(self.session, goals, important, self.root)

    def test_repair_is_meaningful_once_and_detailed_evidence_stays_in_terminal(self):
        evidence = {'acceptance': {ROWS[0]: {'criterion': 'Changing the threshold updates highlighted moments'}},
                    'artifact': {'locator': '#threshold', 'http': 200, 'observed': 'saved result unchanged'}}
        verify = Recorder({'passed': False, 'reason': 'the saved result does not update when the threshold changes', 'evidence': evidence},
                          {'passed': False, 'reason': 'the saved result does not update when the threshold changes', 'evidence': evidence},
                          {'passed': True, 'reason': 'it works', 'evidence': evidence})
        orch = self.orchestrator(verify=verify)
        orch.build_requested(PIECE, ROWS)
        self.assertEqual([], self.messages())
        orch.build_finished(PIECE, 'idle', ROWS)
        self.assertEqual('fixing', self.phase()['status'])
        self.assertEqual(1, len(self.messages()))
        self.assertIn('saved result does not update', self.messages()[0]['text'])
        orch.build_finished(PIECE, 'idle', ROWS)
        self.assertEqual(1, len(self.messages()))
        orch.build_finished(PIECE, 'idle', ROWS)
        self.assertEqual('done', self.phase()['status'])
        self.assertEqual(2, len(self.messages()))
        self.assertIn('Changing the threshold', self.messages()[-1]['text'])
        self.assertNotIn('locator', str(self.messages()))
        self.assertIn('#threshold', str(BUILD.load_activity(self.session, self.root, PIECE)))
        COMM.publish(self.session, self.root, PIECE, 'done', 'Duplicate poll')
        self.assertEqual(2, len(self.messages()))

    def test_human_build_question_is_delivered_once_and_answer_resumes(self):
        self.question_row()
        chat = Recorder(_answer(needs={'kind': 'human_preference', 'question': 'Use time since edit or successful run?'}),
                        dict(_answer(), resolution='resume'))
        orch = self.orchestrator(chat=chat)
        orch.build_finished(PIECE, 'waiting', ROWS)
        self.assertEqual('needs_user', self.phase()['status'])
        self.assertEqual([ROWS[0]], self.phase()['todoIds'])
        self.assertEqual(1, len(self.messages()))
        with mock.patch.object(self.runtime, 'answer', return_value={'ok': True}) as resume:
            answer = orch.bart_message(self.held(), [{'role': 'you', 'text': 'Use time since the last successful run'}])
        self.assertTrue(answer['ok'])
        self.assertEqual('building', self.phase()['status'])
        self.assertEqual(ROWS[0], resume.call_args.args[3])
        self.assertIn('successful run', resume.call_args.args[4])

    def test_fast_resumed_verification_is_not_overwritten_by_building(self):
        self.question_row()
        chat = Recorder(_answer(needs={"kind": "human_preference", "question": "Which definition?"}),
                        dict(_answer(), resolution="resume"))
        orch = self.orchestrator(chat=chat, verify=Recorder({"passed": True, "reason": "correct", "evidence": {}}))
        orch.build_finished(PIECE, "waiting", ROWS)
        def finish(*args):
            mark_rows(self.session, self.root, PIECE, ROWS, "done")
            orch.build_finished(PIECE, "idle", ROWS)
            return {"ok": True}
        with mock.patch.object(self.runtime, "answer", side_effect=finish):
            orch.bart_message(self.held(), [{"role": "you", "text": "Use edit time"}])
        self.assertEqual("done", self.phase()["status"])

    def test_cold_answer_preserves_rows_and_criteria_for_reverification(self):
        self.question_row()
        record = {"claude_session_id": "saved-session", "cwd": str(self.project),
                  "picked": list(ROWS), "verification_rows": list(ROWS),
                  "acceptance": {ROWS[0]: {"criterion": "Saved result updates"}}}
        with mock.patch.object(BUILD, "_run_for", return_value=None), \
             mock.patch.object(BUILD, "load_run", return_value=record), \
             mock.patch.object(BUILD, "_RUNS", {}), \
             mock.patch.object(BUILD.Run, "spawn", autospec=True) as spawn:
            self.assertTrue(BUILD.answer(self.session, self.root, PIECE, ROWS[0], "Use edit time")["ok"])
            run = spawn.call_args.args[0]
            self.assertEqual(list(ROWS), run.picked)
            self.assertEqual(list(ROWS), run.verification_rows)
            self.assertEqual(record["acceptance"], run.acceptance)

    def test_progress_question_does_not_resume_a_waiting_build(self):
        self.question_row()
        chat = Recorder(_answer(needs={'kind': 'human_preference', 'question': 'Which definition?'}),
                        dict(_answer('Waiting for your definition.'), resolution='wait'))
        orch = self.orchestrator(chat=chat)
        orch.build_finished(PIECE, 'waiting', ROWS)
        with mock.patch.object(self.runtime, 'answer') as resume:
            orch.bart_message(self.held(), [{'role': 'you', 'text': 'What are you doing?'}])
        resume.assert_not_called()
        self.assertEqual('needs_user', self.phase()['status'])

    def test_environment_build_question_is_discovered_without_needs_you(self):
        self.question_row()
        orch = self.orchestrator(chat=Recorder(_answer(needs={'kind': 'environment', 'question': 'Where are the data columns?'})))
        with mock.patch.object(self.runtime, 'answer', return_value={'ok': True}) as resume:
            self.assertTrue(orch.build_finished(PIECE, 'waiting', ROWS))
        self.assertEqual('building', self.phase()['status'])
        self.assertEqual([], self.messages())
        self.assertTrue(self.runtime.discoveries)
        self.assertIn('Local inspection', resume.call_args.args[4])
        self.assertNotIn('chat.needs_human', self.events())

    def test_conversation_preference_leaves_needs_you_after_answer(self):
        chat = Recorder(_answer(needs={'kind': 'human_preference', 'question': 'CSV or parquet?'}), dict(_answer('Parquet it is.'), resolution='resume'))
        brain = Recorder({'ok': True, 'card': 'questions', 'replies': [{'kind': 'text', 'text': 'CSV or parquet?'}]})
        orch = self.orchestrator(chat=chat, brainstorm=brain)
        answer = orch.bart_message(self.held(), [{'role': 'you', 'text': 'Export it'}])
        self.assertEqual('needs_user', self.phase()['status'])
        self.assertIn('CSV or parquet?', str(answer['replies']))
        orch.bart_message(self.held(), [{'role': 'you', 'text': 'Use parquet'}])
        self.assertIsNone(self.phase())

    def test_repeated_dependency_clears_to_the_prior_phase(self):
        orch = self.orchestrator()
        for _ in range(2):
            orch.emit('chat.needs_human', EV.AGENT,
                      {'question': 'CSV or parquet?', 'resume': 'conversation'}, subgoal_id=PIECE)
        orch.emit('human.answered', EV.USER, {}, subgoal_id=PIECE)
        self.assertIsNone(self.phase())

    def test_conversation_progress_question_keeps_dependency(self):
        orch = self.orchestrator(chat=Recorder(dict(_answer('Waiting for your choice.'), resolution='wait')))
        orch.emit('chat.needs_human', EV.AGENT,
                  {'question': 'CSV or parquet?', 'resume': 'conversation'}, subgoal_id=PIECE)
        orch.bart_message(self.held(), [{'role': 'you', 'text': 'What are you doing?'}])
        self.assertEqual('needs_user', self.phase()['status'])

    def test_resume_exception_keeps_human_question_and_details_in_terminal(self):
        self.question_row()
        orch = self.orchestrator(chat=Recorder(
            _answer(needs={'kind': 'human_preference', 'question': 'Which definition?'}),
            dict(_answer(), resolution='resume')))
        orch.build_finished(PIECE, 'waiting', ROWS)
        with mock.patch.object(self.runtime, 'answer', side_effect=RuntimeError('process unavailable')):
            result = orch.bart_message(self.held(), [{'role': 'you', 'text': 'Use edit time'}])
        self.assertFalse(result['ok'])
        self.assertEqual('needs_user', self.phase()['status'])
        self.assertIn('process unavailable', str(BUILD.load_activity(self.session, self.root, PIECE)))

    def test_technical_failure_is_not_dumped_in_bart(self):
        orch = self.orchestrator(verify=Recorder({'passed': False, 'reason': 'Playwright locator("#export") timeout HTTP 200',
                                                  'evidence': {'expected': 'Export', 'found': False}}))
        orch.build_requested(PIECE, ROWS)
        orch.build_finished(PIECE, 'idle', ROWS)
        text = self.messages()[0]['text']
        self.assertIn('Write the file', text)
        for token in ('Playwright', 'locator', 'HTTP', '200'):
            self.assertNotIn(token, text)


class LifecycleBrowserTests(BrowserCase):
    def test_existing_rows_show_the_real_loop_without_new_layout(self):
        goal, subs = seed_design(self.chat)
        rows = [r['id'] for r in GM.by_id(self.goals()[0], subs[0])['todo_items']]
        orch = ORCH.Orchestrator('chat', self.root)
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            expect = self.expect
            expect(page.get_by_label('Todo', exact=True).first).to_be_visible()
            rail = page.locator('.rail').bounding_box()
            for kind, label in [('build.started', 'Building…'), ('verify.started', 'Checking…'),
                                ('build.repair_requested', 'Fixing…'), ('verify.started', 'Checking…')]:
                orch.emit(kind, EV.SYSTEM, {'rows': rows}, subgoal_id=subs[0])
                expect(page.locator('.todo-status').first).to_have_text(label, timeout=7000)
                page.get_by_label('Message Bart').fill('I can still type while work runs')
                expect(page.get_by_role('button', name='Send')).to_have_attribute('aria-disabled', 'false')
                expect(page.locator('.from-bart')).to_have_count(0)
            page.screenshot(path='/private/tmp/engelbart-checking.png')
            orch.emit('chat.needs_human', EV.AGENT, {'rows': [rows[0]], 'question': 'Use edit time or run time?'}, subgoal_id=subs[0])
            COMM.publish('chat', self.root, subs[0], 'question', 'Use edit time or run time?')
            expect(page.locator('.todo-status').first).to_have_text('Needs you', timeout=7000)
            expect(page.locator('.from-bart .bubble')).to_have_text('Use edit time or run time?')
            page.screenshot(path='/private/tmp/engelbart-needs-you.png')
            orch.emit('human.answered', EV.USER, {}, subgoal_id=subs[0])
            orch.emit('verify.passed', EV.SYSTEM, {'rows': rows}, subgoal_id=subs[0])
            expect(page.locator('.todo-status').first).to_have_text('Done', timeout=7000)
            expect(page.locator('.todo-mark').first).to_have_text('✓')
            self.assertEqual(rail['width'], page.locator('.rail').bounding_box()['width'])
            self.assertEqual([], errors)

    def test_composer_answer_uses_the_existing_build_resume_contract(self):
        goal, subs = seed_design(self.chat)
        goals, important = self.goals()
        row = GM.by_id(goals, subs[0])["todo_items"][0]
        row.update(status="asking", question="Use edit time or successful run time?")
        CS.save_goals("chat", goals, important, self.root)
        chat = Recorder(_answer(needs={"kind": "human_preference", "question": row["question"]}),
                        dict(_answer(), resolution="resume"))
        orch = ORCH.Orchestrator("chat", self.root, agents={"chat": chat})
        orch.build_finished(subs[0], "waiting", [row["id"]])
        with mock.patch.object(ORCH, "for_chat", return_value=orch), \
             mock.patch.object(orch.runtime, "answer", return_value={"ok": True}) as resume, \
             server_for(self.chat) as url, self.page_on(url) as (page, errors):
            self.expect(page.locator(".todo-status").first).to_have_text("Needs you", timeout=7000)
            self.expect(page.locator(".from-bart .bubble")).to_have_text(row["question"])
            page.get_by_label("Message Bart").fill("Use time since the last successful run")
            page.get_by_label("Message Bart").press("Enter")
            self.expect(page.locator(".todo-status").first).to_have_text("Building…", timeout=7000)
            resume.assert_called_once()
            self.assertIn("successful run", resume.call_args.args[-1])
            self.assertEqual([], errors)
