"""Production conversation clearing and bounded automatic preview startup."""
from unittest import mock
from test_goal_page import BrowserCase, seed_design, bind_project, server_for, CS, GM, ui
from human_compact.trajectory import preview as PV

class WorkspaceConvenienceTests(BrowserCase):
    def setUp(self):
        super().setUp()
        self.goal, self.subs = seed_design(self.chat)
        self.cwd = bind_project(self)

    def test_clear_persists_including_system_messages_and_rejects_stale_save(self):
        old=[{'id':'user-old','who':'you','kind':'text','text':'Hello'}]
        CS.save_bart_chat('chat',self.subs[0],old,self.root)
        CS.append_bart_message('chat',self.subs[0],'Old run update',self.root,message_id='sys-old')
        stale=CS.load_bart_chats('chat',self.root)[self.subs[0]]
        before=CS.load_goals('chat',self.root)
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            self.expect(page.locator('.feed .msg')).to_have_count(2)
            page.get_by_role('button',name='Clear conversation').click()
            self.expect(page.locator('.feed .msg')).to_have_count(0)
            self.expect(page.get_by_role('button',name='Clear conversation')).to_be_disabled()
            CS.save_bart_chat('chat',self.subs[0],stale,self.root,merge=True)
            self.assertEqual([],CS.load_bart_chats('chat',self.root).get(self.subs[0],[]))
            page.reload()
            self.expect(page.locator('.feed .msg')).to_have_count(0)
            CS.append_bart_message('chat',self.subs[0],'A new question',self.root,message_id='sys-new')
            self.expect(page.locator('.feed')).to_contain_text('A new question')
            self.assertEqual(before,CS.load_goals('chat',self.root))
            self.assertEqual([],errors)

    def test_opening_bart_autostarts_safe_preview_once_without_tab_click(self):
        state={'ok':True,'cwd':str(self.cwd),'status':'ready','autostart':True,'detected_at':'now'}
        def start(*args,**kwargs):
            self.assertTrue(kwargs['auto'])
            state.update(status='running',run={'running':True,'healthy':True,'url':'http://127.0.0.1:8000'})
            return {'ok':True}
        with mock.patch.object(ui,'_preview_state',side_effect=lambda *a,**kw:dict(state)), mock.patch.object(PV,'show_ui',side_effect=start) as start, server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.wait_for_function("window.engelbart?.store.get().panes?.preview?.status === 'running'")
            self.expect(page.get_by_role('tab',name='Bart',exact=True)).to_have_attribute('aria-selected','true')
            page.evaluate('window.engelbart.actions.loadPanes()')
            self.assertEqual(1,start.call_count)
            self.assertEqual([],errors)

    def test_stopped_preview_stays_stopped_and_refused_auto_is_not_retried(self):
        state={'ok':True,'cwd':str(self.cwd),'status':'ready','autostart':False}
        with mock.patch.object(ui,'_preview_state',side_effect=lambda *a,**kw:dict(state)), mock.patch.object(PV,'show_ui',return_value={'ok':False,'error':'not eligible to start unasked'}) as start, server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.wait_for_function("window.engelbart?.store.get().panes?.preview?.status === 'ready'")
            start.assert_not_called()
            state['autostart']=True
            page.evaluate('window.engelbart.actions.loadPanes()')
            page.wait_for_function("window.engelbart.store.get().previewBusy === false")
            page.evaluate('window.engelbart.actions.loadPanes()')
            self.assertEqual(1,start.call_count)
            self.expect(page.get_by_role('tabpanel')).not_to_contain_text('not eligible')
            self.assertEqual([],errors)
