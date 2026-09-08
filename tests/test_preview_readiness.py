"""The mutable build stays hidden until actual artifact evidence permits reveal."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from test_agents import AgentCase, PIECE, ROWS, mark_rows
from human_compact.trajectory import build as B
from human_compact.trajectory.agents import artifacts as A, runtime as RT, verifier as V

CONTROL = {'kind':'control', 'role':'button', 'name':'Export'}
CONTRACT = {'criterion':'Export button is visible', 'coverage':'complete', 'checks':[CONTROL]}


class ReadinessTests(AgentCase):
    def seed(self, token='first', status='idle', goal=PIECE):
        B._save_run(self.session, self.root, {
            'goal_id':goal, 'cwd':str(self.project), 'status':status, 'phase':'rows',
            'acceptance':{r:CONTRACT for r in ROWS},
            'preview_readiness':{'token':token,'created_at':token,'status':'preparing'}})
        mark_rows(self.session, self.root, PIECE, ROWS, 'done')

    def gate(self):
        return B.preview_readiness(self.session,self.root,str(self.project))['status']

    def test_gate_is_persisted_before_writer_spawn(self):
        run=B.Run(self.session,self.root,PIECE,str(self.project),'test-run')
        run.acceptance={ROWS[0]:CONTRACT}
        def spawn(*args,**kwargs):
            self.assertEqual('preparing',self.gate())
            raise OSError('fixture spawn stopped')
        with mock.patch.object(B,'_trust_folder'), mock.patch.object(B,'relevant_live_process',return_value=False), mock.patch.object(run,'_command',return_value=['fixture']), mock.patch.object(B.subprocess,'Popen',side_effect=spawn):
            with self.assertRaises(OSError): run.spawn('build',False)

    def test_cancelled_build_is_held_not_forever_preparing(self):
        self.seed(status='cancelled')
        self.assertEqual('held',self.gate())

    def test_ready_survives_reload_but_new_writer_invalidates_it(self):
        self.seed()
        B.set_preview_readiness(self.session,self.root,PIECE,'first','ready')
        self.assertEqual('ready',self.gate())
        self.seed('second','running')
        B.set_preview_readiness(self.session,self.root,PIECE,'first','ready')
        self.assertEqual('preparing',self.gate())
        self.assertEqual('second', B.load_run(self.session,self.root,PIECE)['preview_readiness']['token'])

    def test_inactive_subgoal_writer_blocks_project_preview(self):
        self.seed('first','running')
        self.seed('second','idle','other')
        B.set_preview_readiness(self.session,self.root,'other','second','ready')
        self.assertEqual('preparing',self.gate())

    def test_legacy_without_gate_keeps_existing_behavior(self):
        self.assertIsNone(B.preview_readiness(self.session,self.root,str(self.project)))

    def test_browser_pass_reveals_before_remaining_verification_without_duplicate_browser(self):
        self.seed()
        rt=RT.LocalRuntime(str(self.project),self.root)
        preview={'status':'running','url':'http://127.0.0.1:9999','healthy':True}
        later=[]
        def extra(_runtime):
            later.append(self.gate())
            return True, 'Additional check passed'
        with mock.patch.object(rt,'preview_state',return_value=preview), mock.patch.object(A,'inspect_page',return_value={'passed':True}) as browser:
            verdict=V.verify(self.session,self.root,PIECE,ROWS,rt,[extra])
        self.assertTrue(verdict['passed'])
        self.assertEqual(['ready'],later)
        self.assertEqual('ready',self.gate())
        browser.assert_called_once()

    def test_wrong_artifact_never_revealed_and_final_failure_revokes_ready(self):
        for browser_pass in (False,True):
            self.seed()
            rt=RT.LocalRuntime(str(self.project),self.root)
            with mock.patch.object(rt,'preview_state',return_value={'status':'running','url':'http://127.0.0.1:9999'}), mock.patch.object(A,'inspect_page',return_value={'passed':browser_pass,'reason':'Wrong UI'}):
                verdict=V.verify(self.session,self.root,PIECE,ROWS,rt,[lambda _: (False,'Failed additional check')])
            self.assertFalse(verdict['passed'])
            self.assertEqual('held',self.gate())

    def test_partial_contract_waits_for_semantic_evidence(self):
        rt=RT.LocalRuntime(str(self.project),self.root)
        seen=[]
        class Engine:
            def generate_json(_self,prompt):
                self.assertEqual([],seen)
                return {'passed':False,'reason':'Missing interaction','evidence':[]}
        with mock.patch.object(A,'inspect_page',return_value={'passed':True}):
            result=A.verify(rt,{'row':dict(CONTRACT,coverage='partial',unverified=['Export saves the correct file'])}, {'url':'http://127.0.0.1:9999'},engine=Engine(),on_ready=lambda:seen.append(True))
        self.assertFalse(result['passed'])
        self.assertEqual([],seen)

    def test_blank_and_crashed_pages_fail_even_if_http_is_healthy(self):
        import importlib.util
        import os
        if importlib.util.find_spec('playwright') is None:
            if os.environ.get('ENGELBART_REQUIRE_BROWSER_TESTS') == '1':
                self.fail('The dedicated browser gate requires Playwright')
            self.skipTest('Playwright runs in the dedicated browser gate')
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*a): pass
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(b'<button>Export</button><script>throw Error("broken")</script>' if self.path=='/crash' else b'<html><body></body></html>')
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            for path,checks,reason in [('/blank',[],'blank'),('/crash',[CONTROL],'runtime error')]:
                result=A.inspect_page('http://127.0.0.1:%d%s'%(server.server_port,path),checks)
                self.assertFalse(result['passed']);self.assertIn(reason,result['reason'])
        finally:
            server.shutdown();server.server_close();thread.join()

from test_goal_page import BrowserCase, seed_design, server_for
from test_project_resources import bind_project
from human_compact.trajectory import chat_state as CS, preview as PV
from human_compact.trajectory.agents import events as EV


class ProductionReadinessTests(BrowserCase):
    def test_production_hides_unchecked_frame_then_reveals_while_checking_and_repair_hides_it(self):
        _,subs=seed_design(self.chat)
        cwd=bind_project(self)
        manifest=CS.load_manifest('chat',self.root)
        manifest['project_home']=str(cwd)
        CS.paths('chat',self.root).manifest.write_text(json.dumps(manifest))
        B._save_run('chat',self.root,{'goal_id':subs[0],'cwd':str(cwd),'status':'idle',
            'preview_readiness':{'token':'one','created_at':'one','status':'preparing'}})
        state={'ok':True,'status':'running','url':'http://127.0.0.1:9876','run':{'healthy':True,'command':'fixture'}}
        def event(kind):
            EV.record('chat',self.root,EV.new_event(kind,'system',{'rows':[]},subgoal_id=subs[0]))
        with mock.patch.object(PV,'state',side_effect=lambda *a,**k:dict(state)), mock.patch.object(PV,'verify_running'), server_for(self.chat) as url,self.page_on(url) as (page,errors):
            # Use bounded fixture content for the iframe only; workspace API,
            # persisted build gate and lifecycle event projection remain real.
            page.route('http://127.0.0.1:9876/**',lambda route:route.fulfill(status=200,content_type='text/html',body='<button>Export</button>'))
            event('verify.started')
            page.get_by_role('tab',name='Live preview',exact=True).click()
            self.expect(page.get_by_role('status').filter(has_text='Preparing preview')).to_be_visible()
            self.expect(page.locator('iframe.preview-frame')).to_have_count(0)
            page.reload()
            page.get_by_role('tab',name='Live preview',exact=True).click()
            self.expect(page.locator('iframe.preview-frame')).to_have_count(0)
            B.set_preview_readiness('chat',self.root,subs[0],'one','ready')
            self.expect(page.locator('iframe.preview-frame')).to_have_attribute('src',state['url'])
            # This does not emit verify.passed or complete any TODO.
            self.assertEqual('ready',B.preview_readiness('chat',self.root,str(cwd))['status'])
            from human_compact.trajectory import ui
            self.assertEqual('checking',ui._goal_page_build_phase('chat',self.root,subs[0])['status'])
            event('build.repair_requested')
            B.set_preview_readiness('chat',self.root,subs[0],'one','held')
            self.expect(page.locator('iframe.preview-frame')).to_have_count(0)
            self.expect(page.get_by_role('tabpanel')).to_contain_text('Preview is not ready yet.')
            self.assertEqual([],errors)
