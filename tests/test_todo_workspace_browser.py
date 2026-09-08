"""Production /: wrapping, local resizers, row execution and explicit completion."""
import json
from unittest import mock
from test_goal_page import BrowserCase, seed_design, server_for, CS, GM, ui, post_json, bind_project
from human_compact.trajectory import build as BUILD
from human_compact.trajectory.agents import acceptance, events as EV, chat as CHAT


class TodoWorkspaceBrowserTests(BrowserCase):
    def setUp(self):
        super().setUp()
        patch=mock.patch.object(acceptance,'prepare');patch.start();self.addCleanup(patch.stop)
        self.goal,self.subs=seed_design(self.chat)
        self.project=bind_project(self)
        goals,important=self.goals()
        self.rows=GM.by_id(goals,self.subs[0])['todo_items']
        for row in self.rows: row['acceptance']={'criterion':'A file exists','coverage':'complete','checks':[{'kind':'file_exists','path':'index.html'}]}
        CS.save_goals('chat',goals,important,self.root)

    def emit(self,kind,ids=None,sub=None):
        EV.record('chat',self.root,EV.new_event(kind,'system',{'rows':ids or [r['id'] for r in self.rows]},subgoal_id=sub or self.subs[0]))

    def test_long_todo_wraps_editable_and_held_without_overflow(self):
        text='Create a window with two empty text boxes side by side, label the left Therapist Instruction and the right Generated Software. '+('verylongpathsegment'*20)
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            row=page.locator('.todo-entry').first
            field=row.get_by_role('textbox',name='Todo',exact=True);field.fill(text)
            self.expect(field).to_have_value(text)
            page.wait_for_function("document.querySelector('textarea.todo-text').clientHeight > 60")
            self.assertTrue(field.evaluate('(el)=>el.scrollHeight<=el.clientHeight+1 && el.scrollWidth<=el.clientWidth+1'))
            self.expect(row.get_by_role('button',name='Remove todo')).to_be_visible()
            self.expect(row.get_by_role('button',name='Build todo: '+text,exact=True)).to_be_visible()
            field.press('Enter');self.expect(field).to_have_value(text)
            self.emit('build.started',[self.rows[0]['id']])
            self.expect(row.locator('.todo-status')).to_have_text('Building…')
            self.expect(field).to_have_attribute('readonly','')
            self.assertTrue(field.evaluate('(el)=>el.scrollWidth<=el.clientWidth+1'))
            self.assertEqual([],errors)

    def test_local_dividers_drag_keyboard_reset_and_persist(self):
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.get_by_role('button',name='Build all',exact=True)).to_be_visible()
            plan=page.get_by_role('separator',name='Plan width');before=page.get_by_label('Plan',exact=True).bounding_box()['width']
            box=plan.bounding_box();page.mouse.move(box['x'],box['y']+80);page.mouse.down();page.mouse.move(box['x']+80,box['y']+80);page.mouse.up()
            after=page.get_by_label('Plan',exact=True).bounding_box()['width'];self.assertGreater(after,before+50)
            page.reload();self.expect(plan).to_be_visible();self.assertAlmostEqual(after,page.get_by_label('Plan',exact=True).bounding_box()['width'],delta=2)
            plan.focus();plan.press('ArrowLeft');self.assertLess(page.get_by_label('Plan',exact=True).bounding_box()['width'],after)
            plan.dblclick()
            split=page.get_by_role('separator',name='Conversation and Todos width');box=split.bounding_box();left=page.locator('.brainstorm').bounding_box()['width']
            page.mouse.move(box['x'],box['y']+80);page.mouse.down();page.mouse.move(box['x']-90,box['y']+80);page.mouse.up()
            width=page.locator('.brainstorm').bounding_box()['width'];self.assertLess(width,left-50)
            page.get_by_role('button',name='Hide todos').click();self.expect(split).to_have_count(0)
            page.get_by_role('button',name='Show todos').click();self.assertAlmostEqual(width,page.locator('.brainstorm').bounding_box()['width'],delta=2)
            page.reload();self.expect(split).to_be_visible();self.assertAlmostEqual(width,page.locator('.brainstorm').bounding_box()['width'],delta=2)
            page.set_viewport_size({'width':600,'height':900});self.expect(plan).not_to_be_visible();self.expect(split).not_to_be_visible()
            self.assertEqual([],errors)

    def test_second_todo_build_keeps_logs_in_terminal(self):
        started=[]
        def start(session,root,goal,ids,quick=False):
            started.append(list(ids))
            goals,important=CS.load_goals(session,root)
            for row in GM.by_id(goals,goal)['todo_items']:
                if row['id'] in ids:row['status']='building'
            CS.save_goals(session,goals,important,root)
            return {'ok':True,'rows':list(ids)}
        with mock.patch.object(BUILD,'start',side_effect=start), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.locator('.todo-build')).to_have_count(2)
            self.expect(page.get_by_role('button',name='Build all',exact=True)).to_be_visible()
            page.locator('.todo-build').nth(1).click()
            second=page.locator('.todo-entry').nth(1);first=page.locator('.todo-entry').first
            self.expect(second.locator('.todo-status')).to_have_text('Building…')
            self.expect(first.locator('.todo-status')).to_have_count(0)
            page.wait_for_function("window.engelbart.store.get().building === null")
            self.assertEqual([[self.rows[1]['id']]],started)
            BUILD.note_activity('chat',self.root,self.subs[0],'tool','edited app.js')
            self.expect(page.get_by_label('Build activity')).to_have_count(0)
            self.expect(page.locator('.todos')).not_to_contain_text('edited app.js')
            self.expect(page.get_by_role('button',name='Build all',exact=True)).to_be_visible()
            self.expect(page.locator('.todos .todo-activity')).to_have_count(0)
            self.expect(first.get_by_label('Build activity')).to_have_count(0)
            for kind,label in [('verify.started','Checking…'),('build.repair_requested','Fixing…'),('verify.started','Checking…')]:
                self.emit(kind,[self.rows[1]['id']]);self.expect(second.locator('.todo-status')).to_have_text(label)
            self.emit('verify.passed',[self.rows[1]['id']]);self.expect(second.locator('.todo-status')).to_have_text('Done');self.expect(page.get_by_label('Build activity')).to_have_count(0)
            page.get_by_role('tab',name='Terminal',exact=True).click()
            self.expect(page.get_by_role('tabpanel')).to_contain_text('edited app.js')
            self.assertEqual([],errors)

    def test_build_all_label_tracks_explicit_request_and_all_row_scope(self):
        with mock.patch.object(BUILD, 'start', return_value={'ok': True}), server_for(self.chat) as url, self.page_on(url) as (page, errors):
            button = page.locator('.todos-actions .build-btn')
            self.expect(button).to_contain_text('Build all')
            button.click()
            self.expect(button).to_contain_text('Building…')
            page.reload()
            self.expect(button).to_contain_text('Building…')
            self.emit('verify.started', [row['id'] for row in self.rows])
            self.expect(button).to_contain_text('Building…')
            self.emit('verify.passed', [row['id'] for row in self.rows])
            self.expect(button).to_contain_text('Build all')
            self.assertEqual([], errors)

    def test_double_click_plan_title_renames_persistently_without_completion(self):
        original=self.rows_title()
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            title=page.get_by_role('button',name=original,exact=True)
            title.click()
            self.expect(page.get_by_role('textbox',name='Rename subgoal')).to_have_count(0)
            title.dblclick()
            field=page.get_by_role('textbox',name='Rename subgoal')
            self.expect(field).to_be_focused()
            field.fill('Inspect one real session');field.press('Enter')
            self.expect(page.get_by_role('button',name='Inspect one real session',exact=True)).to_be_visible()
            page.reload()
            title=page.get_by_role('button',name='Inspect one real session',exact=True)
            self.expect(title).to_be_visible()
            self.expect(page.get_by_role('button',name='Complete subgoal: Inspect one real session')).to_have_attribute('aria-pressed','false')
            title.dblclick();field.fill('Discard this title');field.press('Escape')
            self.expect(title).to_be_visible()
            title.dblclick();field.fill('Compare one session')
            page.get_by_role('tab',name='Terminal',exact=True).click()
            self.expect(page.get_by_role('button',name='Compare one session',exact=True)).to_be_visible()
            self.assertEqual([],errors)

    def test_subgoal_completion_icons_selection_guard_and_reload(self):
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.get_by_role('button',name='Complete goal',exact=True)).to_have_count(0)
            self.expect(page.get_by_role('button',name='Reopen goal',exact=True)).to_have_count(0)
            complete=page.get_by_role('button',name='Complete subgoal: '+self.rows_title(),exact=True)
            self.expect(complete).to_have_attribute('aria-pressed','false')
            self.expect(complete).to_have_text('')
            self.assertGreater(float(complete.evaluate('(el)=>parseFloat(getComputedStyle(el).borderTopWidth)')),0)
            footprint=complete.bounding_box()
            # Selecting another title never toggles its completion. Completing
            # the first subgoal must not steal that selection either.
            second=page.locator('.sub-title').nth(1);second.click()
            self.expect(second).to_have_attribute('aria-current','true')
            self.expect(page.locator('.sub-mark[aria-pressed="true"]')).to_have_count(0)
            complete.click()
            reopen=page.get_by_role('button',name='Reopen subgoal: '+self.rows_title(),exact=True)
            self.expect(reopen).to_have_attribute('aria-pressed','true')
            self.expect(reopen).to_have_text('✓')
            self.expect(second).to_have_attribute('aria-current','true')
            self.assertEqual('0px',reopen.evaluate('(el)=>getComputedStyle(el).borderTopWidth'))
            for dimension in ['width','height']:
                self.assertEqual(footprint[dimension],reopen.bounding_box()[dimension])
            page.reload();self.expect(reopen).to_be_visible()
            reopen.click();self.expect(complete).to_be_visible()
            page.reload();self.expect(complete).to_have_attribute('aria-pressed','false')
            for kind in ['build.started','verify.started','build.repair_requested','chat.needs_human']:
                self.emit(kind);self.expect(complete).to_be_disabled()
                answer=post_json(url+'/api/goal-page/op',{'op':'set_status','goal_id':self.subs[0],'status':'completed'},{'Origin':url})
                self.assertFalse(answer['ok'])
            self.assertEqual([],errors)

    def test_completed_top_goal_has_no_header_completion_control(self):
        goals,important=self.goals();GM.by_id(goals,self.goal)['status']='completed'
        CS.save_goals('chat',goals,important,self.root)
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.locator('.header')).to_be_visible()
            self.expect(page.get_by_role('button',name='Complete goal',exact=True)).to_have_count(0)
            self.expect(page.get_by_role('button',name='Reopen goal',exact=True)).to_have_count(0)
            self.expect(page.locator('button.sub-mark')).to_have_count(len(self.subs))
            self.assertEqual([],errors)

    def rows_title(self):
        goals,_=self.goals();return GM.by_id(goals,self.subs[0])['title']

    def test_hello_and_background_message_channels_remain_distinct_on_reload(self):
        answer={'ok':True,'say':'Hi! g11 is ready.','todos':['Unwanted task'],'needs':{}}
        with mock.patch.object(CHAT,'ask',return_value=answer), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('textbox',name='Message Bart').fill('hello');page.get_by_role('button',name='Send',exact=True).click()
            self.expect(page.locator('.from-bart:not(.is-thinking)')).to_have_count(1)
            self.expect(page.locator('.proposal')).to_have_count(0)
            self.expect(page.locator('.feed')).not_to_contain_text('g11')
            CS.append_bart_message('chat',self.subs[0],'The check found a problem; I am fixing it.',self.root,message_id='sys-life-fixture')
            self.expect(page.locator('[data-channel="lifecycle"]')).to_have_count(1)
            self.expect(page.locator('[data-channel="lifecycle"] .msg-who')).to_have_text('bart · work update')
            page.reload();self.expect(page.locator('[data-channel="lifecycle"]')).to_have_count(1)
            self.expect(page.locator('.from-bart:not(.is-thinking)')).to_have_count(2)
            self.assertEqual([],errors)

    def test_terminal_sections_and_no_old_json_dump(self):
        BUILD.note_activity('chat',self.root,self.subs[0],'error','app.js failed to import module')
        BUILD.note_activity('chat',self.root,self.subs[0],'verify','check evidence: {"acceptance": "g11"}')
        state={'ok':True,'status':'running','run':{'command':'python3 serve.py','lines':['Real application output'],'healthy':True}}
        with mock.patch.object(ui,'_preview_state',return_value=state), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Terminal',exact=True).click()
            self.expect(page.locator('.term-section')).to_have_text(['BUILD','PREVIEW'])
            self.expect(page.get_by_role('tabpanel')).to_contain_text('app.js failed to import module')
            self.expect(page.get_by_role('tabpanel')).to_contain_text('Real application output')
            self.expect(page.get_by_role('tabpanel')).not_to_contain_text('check evidence')
            self.expect(page.get_by_role('tabpanel')).not_to_contain_text('g11')
            self.assertEqual([],errors)
