"""Standard workspace edits keep stable row identities and server-owned state."""
import time
from unittest import mock
from test_goal_page import ChatCase, BrowserCase, seed_design, ui, GM, CS, server_for, wait_for


class StandardRowTests(ChatCase):
    def test_insert_empty_row_at_caret_and_retry_without_duplication(self):
        goal, children = seed_design(self.chat)
        rows = ui._goal_page_payload(self.chat, True, goal)['slices'][children[0]]['todos']
        op = dict(op='insert_todo_row', goal_id=children[0], id='t1234567890ab',
                  text='', depth=1, after_id=rows[0]['id'])
        self.assertTrue(ui._apply(op, self.chat)['ok'])
        self.assertTrue(ui._apply(op, self.chat)['ok'])
        got = ui._goal_page_payload(self.chat, True, goal)['slices'][children[0]]['todos']
        self.assertEqual([rows[0]['id'], op['id'], rows[1]['id']], [r['id'] for r in got])
        self.assertEqual(1, got[1]['depth'])
        self.assertEqual('', got[1]['text'])

    def test_insert_refuses_lost_anchor_and_invalid_identity(self):
        _, children = seed_design(self.chat)
        for rid, anchor in [('t1234567890ab', 'tmissing'), ('bad', '')]:
            result = ui._apply(dict(op='insert_todo_row', goal_id=children[0],
                                   id=rid, text='new', after_id=anchor), self.chat)
            self.assertFalse(result['ok'])

    def test_depth_changes_preserve_status_and_questions_are_visible(self):
        goal, children = seed_design(self.chat)
        state, important = self.goals()
        piece = GM.by_id(state, children[0])
        row = piece['todo_items'][0]
        row.update(status='asking', question='Which dataset should I use?')
        ui._save_goals(self.chat, state, important, True)
        result = ui._apply(dict(op='set_todo_depth', goal_id=children[0], id=row['id'], depth=1), self.chat)
        self.assertFalse(result['ok'])
        got = ui._goal_page_payload(self.chat, True, goal)['slices'][children[0]]['todos'][0]
        self.assertEqual('Which dataset should I use?', got['question'])
        self.assertEqual('asking', got['status'])

    def test_long_todo_text_is_preserved_by_editor(self):
        goal, children = seed_design(self.chat)
        text = 'A requirement with context. ' * 40
        result = ui._apply(dict(op='insert_todo_row', goal_id=children[0],
                               id='t1234567890ab', text=text), self.chat)
        self.assertTrue(result['ok'])
        result = ui._apply(dict(op='set_todo_text', goal_id=children[0],
                               id='t1234567890ab', text=text + 'End.'), self.chat)
        self.assertEqual(text + 'End.', result['row']['text'])


class StandardEditorBrowserTests(BrowserCase):
    def test_build_shortcut_saves_current_notes_text_and_unsubmitted_row(self):
        goal,children=seed_design(self.chat)
        builds=[]
        def start(session_id,root,goal_id,row_ids,quick=False):
            piece=GM.by_id(CS.load_goals(session_id,root)[0],goal_id)
            builds.append((piece['notes'],[r['text'] for r in piece['todo_items'] if r['id'] in row_ids]))
            return {'ok':True,'started':True,'rows':list(row_ids)}
        with mock.patch('human_compact.trajectory.build.start',start), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Notes',exact=True).click()
            page.get_by_label('Subgoal notes').fill('Use the complete dataset.')
            page.get_by_role('tab',name='Bart',exact=True).click()
            page.get_by_label('Todo',exact=True).first.fill('Latest edited requirement')
            page.get_by_label('New todo').fill('A draft that must be built too')
            page.get_by_label('New todo').press('Control+Enter')
            self.assertTrue(wait_for(lambda:bool(builds)))
            self.assertEqual([('Use the complete dataset.',['Latest edited requirement','Add an import button','A draft that must be built too'])],builds)
            self.assertEqual([],errors)

    def test_enter_is_immediate_typing_survives_save_and_indentation_reloads(self):
        goal, children=seed_design(self.chat)
        apply=ui._goal_page_write

        def delayed(body,*args):
            if body.get('op') in ('insert_todo_row','set_todo_text'):
                time.sleep(.4)
            return apply(body,*args)

        with mock.patch.object(ui,'_goal_page_write',delayed), server_for(self.chat) as url, self.page_on(url) as (page,errors):
            todo=page.get_by_label('Todo',exact=True).first
            todo.fill('First second')
            todo.press('Home')
            todo.press('ArrowRight')
            todo.press('ArrowRight')
            todo.press('ArrowRight')
            todo.press('ArrowRight')
            todo.press('ArrowRight')
            todo.press('Enter')
            rows=page.get_by_label('Todo',exact=True)
            self.expect(rows).to_have_count(3,timeout=250)
            self.expect(rows.nth(1)).to_be_focused()
            rows.nth(1).fill('New line typed before the save returns')
            rows.nth(1).press('Tab')
            self.expect(rows.nth(1)).to_be_focused()
            self.expect(rows.nth(1)).to_have_value('New line typed before the save returns')
            self.assertTrue(wait_for(lambda:len(self.goals()[0]['goals'])>0 and any(
                r['text']=='New line typed before the save returns' and r['depth']==1
                for r in GM.by_id(self.goals()[0],children[0])['todo_items'])))
            # A second page sees the same stable rows and depth.
            page.reload(wait_until='domcontentloaded')
            self.expect(rows).to_have_count(3)
            self.expect(rows.nth(1)).to_have_value('New line typed before the save returns')
            self.expect(rows.nth(1).locator('..').locator('..')).to_have_attribute('style','margin-left:24px')
            rows.nth(1).press('Home')
            rows.nth(1).press('ArrowUp')
            self.expect(rows.first).to_be_focused()
            rows.first.press('Control+a')
            self.expect(page.get_by_role('button',name='Build 3',exact=True)).to_be_visible()
            self.assertEqual([],errors)

    def test_notes_save_and_export_include_context_status_and_completed_rows(self):
        goal,children=seed_design(self.chat)
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Notes',exact=True).click()
            page.get_by_label('Subgoal notes').fill('Remember the unit of analysis.')
            self.assertTrue(wait_for(lambda:GM.by_id(self.goals()[0],children[0])['notes']=='Remember the unit of analysis.'))
            page.get_by_role('tab',name='Bart',exact=True).click()
            page.get_by_role('button',name='Mark as done',exact=True).first.click()
            self.assertTrue(wait_for(lambda:GM.by_id(self.goals()[0],children[0])['todo_items'][0]['status']=='done'))
            # Capture the browser's Clipboard API call; do not alter the machine clipboard.
            page.evaluate('window.__copied="";Object.defineProperty(navigator,"clipboard",{value:{writeText:async t=>{window.__copied=t}}})')
            page.get_by_role('button',name='Copy TODOs',exact=True).click()
            text=page.evaluate('window.__copied')
            self.assertIn('Create an interface to import the dataset',text)
            self.assertIn('Create a blank interface with an import button',text)
            self.assertIn('[x] Create a blank interface — Done',text)
            self.assertIn('Remember the unit of analysis.',text)
            page.reload(wait_until='domcontentloaded')
            page.get_by_role('tab',name='Notes',exact=True).click()
            self.expect(page.get_by_label('Subgoal notes')).to_have_value('Remember the unit of analysis.')
            self.assertEqual([],errors)
