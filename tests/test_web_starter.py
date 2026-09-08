"""Preparation, real active data, and a small app running on the existing Preview."""
import io
import json
import os
from pathlib import Path
import threading
import time
from urllib.request import urlopen, Request
from urllib.error import HTTPError
from unittest import mock

from test_agents import AgentCase, PIECE, ROWS
from human_compact.trajectory import starter as S, resources as R, preview as PV, build as B, project_store as PS, chat_state as CS, goals as GM
from human_compact.trajectory.agents import artifacts as A, runtime as RT, acceptance

SMALL = [{'text':'Create a student dropdown and display their timeline'}]


class StarterTests(AgentCase):
    def empty(self):
        folder=self.root/'empty';folder.mkdir(exist_ok=True);return folder

    def test_empty_small_web_project_prepared_once_without_model_or_installs(self):
        cwd=self.empty()
        with mock.patch.object(PV,'_ask_model',side_effect=AssertionError('no model')):
            self.assertTrue(S.prepare(self.root,cwd,SMALL))
        self.assertTrue((cwd/'app.py').is_file())
        self.assertTrue(PV.read_config(self.root,cwd)['profiles'])
        (cwd/'app.js').write_text('user edit')
        self.assertFalse(S.prepare(self.root,cwd,SMALL))
        self.assertEqual('user edit',(cwd/'app.js').read_text())
        self.assertFalse((cwd/'package.json').exists())

    def test_starter_assets_remain_utf8_on_a_windows_codepage(self):
        cwd = self.empty()
        original = Path.read_text
        def windows_read(path, encoding=None, errors=None):
            return original(path, encoding=encoding or 'cp1252', errors=errors)
        with mock.patch.object(Path, 'read_text', windows_read):
            self.assertTrue(S.prepare(self.root, cwd, SMALL))
        for name in ('index.html', 'app.js', 'styles.css', 'ui.js'):
            self.assertEqual((S.ASSETS/name).read_text(encoding='utf-8'),
                             (cwd/name).read_text(encoding='utf-8'))
        self.assertIn('—', (cwd/'ui.js').read_text(encoding='utf-8'))

    def test_existing_code_explicit_framework_risky_and_disabled_are_untouched(self):
        cwd=self.empty();(cwd/'index.html').write_text('existing')
        self.assertFalse(S.prepare(self.root,cwd,SMALL))
        self.assertEqual(['index.html'],[p.name for p in cwd.iterdir()])
        for text in ['Build a React dropdown','Add authentication button','Investigate the entire architecture']:
            with self.subTest(text=text): self.assertFalse(S.prepare(self.root,self.project,[{'text':text}]))
        with mock.patch.dict(os.environ,{'HC_WEB_STARTER':'off'}):
            self.assertFalse(S.prepare(self.root,self.project,SMALL))

    def test_prepared_before_acceptance_and_quick_build_receives_source_brief(self):
        goals,important=CS.load_goals(self.session,self.root)
        rows=GM.by_id(goals,PIECE)['todo_items']
        rows[0]['text']='Create a student dropdown'
        rows[1]['text']='Display their timeline'
        CS.save_goals(self.session,goals,important,self.root)
        complete=threading.Event()
        def ensure(*args,**kwargs):
            self.assertTrue((self.project/'app.js').is_file())
            complete.set()
        with mock.patch.object(acceptance,'ensure',side_effect=ensure):
            acceptance.prepare(self.session,self.root,PIECE)
            self.assertTrue(complete.wait(5))
        with mock.patch.dict(os.environ,{'HC_BUILD_MODE':'headless'}),mock.patch.object(B.Run,'spawn') as spawn:
            result=B.start(self.session,self.root,PIECE,[ROWS[0]],quick=True)
        B._RUNS.pop(f'{self.session}:{PIECE}',None)
        self.assertTrue(result['ok'])
        self.assertIn('FILE app.js',result['prompt'])
        self.assertIn('GET /api/dataset',result['prompt'])
        self.assertEqual([ROWS[0]],result['rows'])
        brief=S.brief(self.project)
        print('Prompt fixture:',json.dumps({'total_chars':len(result['prompt']),'brief_chars':len(brief),'without_brief_chars':len(result['prompt'])-len(brief)}))

    def test_handoff_prepares_starter_before_build_and_stop_is_preserved(self):
        from human_compact.trajectory import web_setup
        payload={'name':'Starter handoff','plan':{'description':'Inspect a student timeline'},
                 'goals':[{'label':'Inspect a student timeline','why':'Compare behavior'}],
                 'chosen':'Inspect a student timeline','todos':[],
                 'subgoals':[{'label':'Inspect one student','todos':['Create a student dropdown','Display their timeline']}]}
        with mock.patch.object(B.Run,'spawn',side_effect=AssertionError('no Build during handoff')):
            result=web_setup.materialize(payload,self.root)
        self.assertTrue(result['ok'],result)
        self.assertTrue((Path(result['cwd'])/'app.js').is_file())
        cwd=self.empty();PV.write_config(self.root,cwd,{'autostart':False})
        self.assertTrue(S.prepare(self.root,cwd,SMALL))
        self.assertFalse(PV.read_config(self.root,cwd)['autostart'])
        self.assertFalse(PV.show_ui(self.root,cwd,auto=True)['ok'])

    def test_starter_runtime_tracks_current_install_and_user_command_opts_out(self):
        cwd=self.empty();S.prepare(self.root,cwd,SMALL)
        with mock.patch.object(S.sys,'executable','/new runtime/python'):
            self.assertEqual("'/new runtime/python' app.py",PV.detect(cwd)[0]['command'])
        (cwd/'Procfile').write_text('web: python custom.py\n')
        self.assertEqual('',S.runtime_command(cwd))

    def test_brief_is_bounded_current_and_skips_secrets_and_external_symlinks(self):
        cwd=self.empty();(cwd/'app.js').write_text('a'*30000)
        (cwd/'.env').write_text('SECRET_SENTINEL')
        outside=self.root/'outside';outside.write_text('OUTSIDE_SENTINEL')
        (cwd/'server.js').symlink_to(outside)
        brief=S.brief(cwd)
        self.assertLess(len(brief),10000)
        self.assertIn('app.js (excerpt)',brief)
        self.assertNotIn('SENTINEL',brief)
        (cwd/'app.js').write_text('current source')
        self.assertIn('current source',S.brief(cwd))

    def upload(self,cwd,name,data):
        return R.upload_dataset(self.root,cwd,name,io.BytesIO(data),len(data))

    def test_active_data_pages_follow_upload_replacement_and_remain_bounded(self):
        cwd=self.empty()
        self.upload(cwd,'original.csv',b'student_id,action\none,edit\n')
        before=R.dataset_rows(self.root,cwd)
        self.upload(cwd,'new.tsv',b'student_id\taction\ntwo\trun\n')
        after=R.dataset_rows(self.root,cwd)
        self.assertNotEqual(before['resource']['id'],after['resource']['id'])
        self.assertEqual('two',after['rows'][0]['student_id'])
        self.assertEqual(2,len(PS.load_project(self.root,cwd)['resources']))
        self.upload(cwd,'many.json',json.dumps([{'n':i} for i in range(450)]).encode())
        first=R.dataset_rows(self.root,cwd)
        self.assertEqual(200,len(first['rows']));self.assertTrue(first['hasMore'])
        last=R.dataset_rows(self.root,cwd,offset=400)
        self.assertEqual(50,len(last['rows']));self.assertFalse(last['hasMore'])
        with self.assertRaises(ValueError):R.dataset_rows(self.root,cwd,limit=999999)

    def test_parquet_xlsx_values_are_real_and_original_files_remain(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        import openpyxl
        cwd=self.empty()
        output=io.BytesIO();pq.write_table(pa.table({'student':['real'], 'value':[42], 'missing':[float('nan')]}),output)
        self.upload(cwd,'actual.parquet',output.getvalue())
        self.assertEqual(42,R.dataset_rows(self.root,cwd)['rows'][0]['value'])
        self.assertEqual('nan',R.dataset_rows(self.root,cwd)['rows'][0]['missing'])
        workbook=openpyxl.Workbook();sheet=workbook.active;sheet.append(['student','value']);sheet.append(['xlsx',23]);sheet.append(['formula','=1+1'])
        output=io.BytesIO();workbook.save(output);workbook.close()
        self.upload(cwd,'actual.xlsx',output.getvalue())
        rows=R.dataset_rows(self.root,cwd)['rows']
        self.assertEqual(23,rows[0]['value']);self.assertIsNone(rows[1]['value'])
        self.assertTrue(list((cwd/'.engelbart-resources').rglob('*.parquet')))
        self.assertTrue(list((cwd/'.engelbart-resources').rglob('*.xlsx')))

    def test_resource_escape_and_ambiguous_alternatives_are_refused(self):
        cwd=self.empty();self.upload(cwd,'one.csv',b'x\n1\n')
        project=PS.load_project(self.root,cwd)
        project['resources'][0]['access']['primaryFiles']=['../secret.csv']
        PS.save_project(self.root,cwd,project)
        with self.assertRaises(ValueError):R.dataset_rows(self.root,cwd)

    def test_real_preview_starts_uses_actual_data_and_checks_requested_ui(self):
        import importlib.util
        import os
        if importlib.util.find_spec('playwright') is None:
            if os.environ.get('ENGELBART_REQUIRE_BROWSER_TESTS') == '1':
                self.fail('The dedicated browser gate requires Playwright')
            self.skipTest('Playwright runs in the dedicated browser gate')
        cwd=self.empty();S.prepare(self.root,cwd,SMALL)
        self.upload(cwd,'data.csv',b'student_id,action\na,edit\nb,run\n')
        # Deterministic builder fixture edits ONE app file, reusing the loader
        # and controls. No model/provider latency is included in these timings.
        (cwd/'app.js').write_text("""import {loadDataset,selectControl,renderTimeline} from './ui.js';
const app=document.querySelector('#app'), data=await loadDataset();
const timeline=document.createElement('div');
const show=id=>timeline.replaceChildren(renderTimeline(data.rows.filter(r=>r.student_id===id),{time:'student_id',label:'action'}));
app.replaceChildren(selectControl('Student',[...new Set(data.rows.map(r=>r.student_id))],show),timeline);show(data.rows[0]?.student_id);""")
        env=mock.patch.dict(os.environ, {'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'hc'/'src')})
        env.start();self.addCleanup(env.stop)
        begin=time.monotonic()
        rt=RT.LocalRuntime(str(cwd),self.root)
        try:
            state=rt.ensure_preview(self.session,timeout=10)
            self.assertTrue(state.get('url'),state)
            started=time.monotonic()
            checks=[{'kind':'control','role':'combobox','name':'Student'}, {'kind':'text','text':'a — edit'}]
            result=A.inspect_page(state['url'],checks)
            self.assertTrue(result['passed'],result)
            self.assertTrue(PV.running(cwd).alive())
            from playwright.sync_api import sync_playwright, expect
            with sync_playwright() as playwright:
                browser=playwright.chromium.launch(headless=True,executable_path=A.browser_executable())
                try:
                    page=browser.new_page();page.goto(state['url'])
                    page.get_by_role('combobox',name='Student').select_option('b')
                    expect(page.get_by_text('b — run')).to_be_visible()
                finally:
                    browser.close()
            with urlopen(state['url']+'/api/dataset') as response:
                self.assertEqual('no-store',response.headers['Cache-Control'])
                self.assertEqual('a',json.load(response)['rows'][0]['student_id'])
            self.upload(cwd,'replacement.json',json.dumps([{'student_id':'c','action':'<script>throw Error("dataset ran")</script>'}]).encode())
            result=A.inspect_page(state['url'],[{'kind':'text','text':'c — <script>throw Error("dataset ran")</script>'}])
            self.assertTrue(result['passed'],result)
            for path in ('/.engelbart-starter.json','/app.py','/../.env'):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(state['url']+path)
                caught.exception.close()
            for headers in ({'Host':'attacker.example'},{'Origin':'https://attacker.example'}):
                with self.assertRaises(HTTPError) as caught:
                    urlopen(Request(state['url']+'/api/dataset',headers=headers))
                self.assertEqual(403,caught.exception.code);caught.exception.close()
            print('Fixture timing:',json.dumps({'preview_start_s':round(started-begin,3),'browser_check_s':round(time.monotonic()-started,3)}))
        finally:
            if PV.running(cwd): PV.running(cwd).stop()
            PV.forget(cwd)

from test_goal_page import BrowserCase, seed_design, server_for


class ProductionStarterTests(BrowserCase):
    def test_production_single_todo_build_reaches_prepared_quick_prompt(self):
        _,subs=seed_design(self.chat)
        cwd=self.root/'new-project';cwd.mkdir()
        manifest=CS.load_manifest('chat',self.root)
        manifest.update(cwd=str(cwd),project_home=str(cwd))
        CS.paths('chat',self.root).manifest.write_text(json.dumps(manifest))
        def derive(rows,*a,**kw):
            return {r['id']:{'criterion':'Requested interface is visible','coverage':'complete',
                'checks':[{'kind':'control','role':'heading','name':'Research workspace'}]} for r in rows}
        spawned=threading.Event()
        with mock.patch.object(acceptance,'derive',side_effect=derive),mock.patch.object(B.Run,'spawn',side_effect=lambda *a,**k:spawned.set()) as spawn,server_for(self.chat) as url,self.page_on(url) as (page,errors):
            with page.expect_response(lambda response: response.request.method == 'POST' and (response.request.post_data_json or {}).get('op') == 'build_todos'):
                page.get_by_role('button',name='Build todo: Create a blank interface',exact=True).click()
            self.expect(page.locator('.todo-status').first).to_have_text('Building…')
            self.assertTrue(spawned.wait(5),'The production Build did not reach the builder')
            self.assertTrue((cwd/'app.js').is_file())
            self.assertIn('FILE app.js',spawn.call_args.args[0])
            record=B.load_run('chat',self.root,subs[0])
            self.assertEqual(1,len(record['picked']))
            self.assertEqual([],errors)
        B._RUNS.pop(f'chat:{subs[0]}',None)
