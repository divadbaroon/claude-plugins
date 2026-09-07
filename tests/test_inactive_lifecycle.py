from test_goal_page import BrowserCase, seed_design, server_for, CS, GM
from human_compact.trajectory.agents import events as EV


class InactiveLifecycleTests(BrowserCase):
    def test_inactive_subgoal_waits_for_verification_success(self):
        _, subs = seed_design(self.chat)
        goals, important = self.goals()
        rows = GM.by_id(goals, subs[0])['todo_items']
        ids = [r['id'] for r in rows]
        def emit(kind):
            EV.record('chat', self.root, EV.new_event(kind, 'system', {'rows': ids}, subgoal_id=subs[0]))
        with server_for(self.chat) as url, self.page_on(url+'/test') as (page, errors):
            a = page.locator('.sub[data-key="'+subs[0]+'"]')
            b = page.locator('.sub[data-key="'+subs[1]+'"]')
            self.expect(a).to_be_visible()
            # Raw builder completion is durable before verification completes.
            for row in rows: row.update(done=True, status='done')
            emit('build.started')
            CS.save_goals('chat', goals, important, self.root)
            emit('verify.started')
            def phase(status):
                page.wait_for_function('([id,status]) => window.engelbart.store.get().phases?.[id]?.status === status', arg=[subs[0],status])
            phase('checking')
            self.assertNotIn('is-complete', a.get_attribute('class'))
            b.click()
            self.assertEqual(subs[1], page.evaluate("window.engelbart.store.get().activeId"))
            self.assertNotIn("is-complete", a.get_attribute("class"))
            for kind, status in [('build.repair_requested','fixing'), ('verify.started','checking'),
                                 ('chat.needs_human','needs_user'), ('build.failed','failed')]:
                emit(kind); phase(status)
                self.assertNotIn('is-complete', a.get_attribute('class'))
                self.assertEqual(subs[1], page.evaluate('window.engelbart.store.get().activeId'))
            emit('verify.started'); phase('checking')
            self.assertNotIn('is-complete', a.get_attribute('class'))
            emit('verify.passed'); phase('done')
            self.assertIn('is-complete', a.get_attribute('class'))
            self.assertEqual(subs[1], page.evaluate('window.engelbart.store.get().activeId'))
            self.assertEqual([], errors)
