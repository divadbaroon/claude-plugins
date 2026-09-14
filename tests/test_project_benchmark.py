import csv
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from human_compact.trajectory import project_benchmark as B

HEADERS = ['Git repo URL', 'Paper DOI', 'Paper Keyword(s)', 'What is in the git repo', 'Types of dependencies in the git repo']

def dataset(rows, extra=None):
    out=io.StringIO(newline=''); w=csv.writer(out); w.writerow(HEADERS+(extra or [])); w.writerows(rows); return out.getvalue()

def row(n, kind='Web application'):
    return [f'https://github.com/example/repo{n}', '10.1234/paper', 'Learning; AI', kind, 'Python; API']

class BenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.home=Path(self.temp.name)
        patch=mock.patch.dict(os.environ, {'HUMAN_COMPACT_HOME':str(self.home)}); patch.start(); self.addCleanup(patch.stop)

    def test_csv_roundtrip_and_errors(self):
        text='\ufeff'+dataset([row(1)+['Line one, "quoted"\nLine two']], ['Evidence'])
        d=B.parse_csv(text, 'sample.csv')
        self.assertEqual(d['cases'][0]['metadata']['Evidence'], 'Line one, "quoted"\nLine two')
        self.assertEqual(d['cases'][0]['dependencies'], ['python','api'])
        self.assertEqual(d['cases'][0]['row'], 2)
        self.assertEqual(d['id'], B.parse_csv(text,'renamed.csv')['id'])
        with self.assertRaisesRegex(ValueError,'row 2'):
            B.parse_csv(dataset([row(1)[:-1]]))
        with self.assertRaisesRegex(ValueError,'row 2'):
            B.parse_csv(','.join(HEADERS)+'\n"unterminated')

    def test_rejects_unsafe_repository_urls(self):
        for url in ['http://github.com/a/b','https://evil.org/a/b','https://u:p@github.com/a/b','https://github.com//b','javascript:alert(1)','https://github.com/a/','https://github.com/a/b;touch','https://github.com/a/b?x=1']:
            with self.subTest(url=url), self.assertRaisesRegex(ValueError,'row 2'):
                B.parse_csv(dataset([[url]+row(1)[1:]]))

    def cohort(self):
        d=B.import_csv(dataset([row(1),row(2),row(3),row(4,'Dataset'),row(5)]),'fixture.csv')
        ids=[c['id'] for c in d['cases']]
        return B.create_batch(d['id'],ids,{'dependency':'python'}),ids

    def test_mixed_batch_fixed_denominator_retry_and_restore(self):
        b,ids=self.cohort()
        def record(case,op,answer):
            token=B.begin({'batchId':b['id'],'caseId':case},op,{})
            B.finish(token,answer)
        record(ids[0],'project_run_state',{'ok':True,'run':{'id':'a'*32,'status':'running','healthy':True,'stages':[{'stage':'start','command':'python app.py','stdout':'listening'}]}})
        record(ids[1],'discover_project_components',{'ok':False,'error':'checkout failed'})
        record(ids[2],'inspect_project_environment',{'ok':True,'variables':[{'name':'KEY','status':'missing','blocksContinuation':True}]})
        record(ids[3],'project_run_state',{'ok':True,'run':{'status':'running','healthy':True}})
        report=B.export_batch(b['id'],refresh=False)
        self.assertEqual(report['summary']['denominator'],5)
        self.assertEqual(sum(report['summary']['counts'].values()),5)
        self.assertEqual([c['outcome'] for c in report['cases']],['healthy_startup','failed','blocked','unsupported','pending'])
        record(ids[1],'project_run_state',{'ok':True,'run':{'status':'running','healthy':True}})
        restored=B.export_batch(b['id'],refresh=False)
        self.assertIn('checkout failed',json.dumps(restored['cases'][1]))
        self.assertEqual(restored['selection']['ids'],ids)
        self.assertEqual(B.latest()['batch']['id'],b['id'])

    def test_environment_values_never_persist_even_public_nested_trace(self):
        b,ids=self.cohort(); ctx={'batchId':b['id'],'caseId':ids[0]}
        token=B.begin(ctx,'save_project_environment',{'values':{'API_KEY':'private-test-token','PUBLIC_NAME':'private-person-name','MULTILINE':'private-line-one\nprivate-line-two'}})
        B.finish(token,{'ok':True,'publicValues':{'PUBLIC_NAME':'private-person-name','MULTILINE':'private-line-one\nprivate-line-two'},'variables':[{'name':'API_KEY','status':'found'}], 'nested':{'agentTrace':{'prompt':'private-test-token and private-person-name','encoded':json.dumps({'data':'private-line-one\nprivate-line-two'})}}})
        raw=''.join(p.read_text() for p in B.folder().glob('*.json'))
        report=json.dumps(B.export_batch(b['id'],refresh=False))
        for value in ['private-test-token','private-person-name','private-line-one']:
            self.assertNotIn(value,raw);self.assertNotIn(value,report)
        self.assertIn('API_KEY',report);self.assertIn('found',report)

    def test_pending_operation_and_bounded_evidence(self):
        b,ids=self.cohort(); ctx={'batchId':b['id'],'caseId':ids[0]}
        token=B.begin(ctx,'analyze_project',{})
        self.assertEqual(B.export_batch(b['id'],refresh=False)['cases'][0]['outcome'],'in_progress')
        B.finish(token,{'ok':False,'error':'x'*40000})
        for i in range(100):
            token=B.begin(ctx,'project_run_state',{});B.finish(token,{'ok':True,'run':{'status':'starting','stdout':str(i)+'x'*40000}})
        report=B.export_batch(b['id'],refresh=False)
        self.assertIn('truncat',json.dumps(report).lower())
        self.assertLess(len(json.dumps(report)),3_000_000)
        with self.assertRaises(ValueError):B.create_batch(b['dataset']['id'], ['invented'],{})

    def test_joined_existing_run_never_becomes_this_cases_startup_success(self):
        batch,ids=self.cohort();ctx={'batchId':batch['id'],'caseId':ids[0]}
        requested='a'*32;borrowed='b'*32
        token=B.begin(ctx,'analyze_project',{})
        B.finish(token,{'ok':True,'analysisId':requested})
        token=B.begin(ctx,'project_run_start',{'id':requested})
        B.finish(token,{'ok':True,'run':{'id':borrowed,'status':'running','healthy':True,'joinedExistingRun':True}})
        token=B.begin(ctx,'project_run_state',{'id':borrowed})
        B.finish(token,{'ok':True,'run':{'id':borrowed,'status':'running','healthy':True}})
        report=B.export_batch(batch['id'],refresh=False);case=report['cases'][0]
        self.assertEqual(case['outcome'],'blocked')
        self.assertEqual(report['summary']['healthyStartup'],0)
        self.assertEqual(case['runId'],requested)
        self.assertEqual(case['execution']['requestedRunId'],requested)
        self.assertEqual(case['execution']['observedRunId'],borrowed)
        self.assertTrue(case['execution']['joinedExistingRun'])
        token=B.begin(ctx,'project_run_start',{'id':requested,'retry':True})
        B.finish(token,{'ok':True,'run':{'id':requested,'status':'running','healthy':True}})
        report=B.export_batch(batch['id'],refresh=False)
        self.assertEqual(report['summary']['healthyStartup'],1)
        self.assertFalse(report['cases'][0]['execution']['joinedExistingRun'])
        self.assertIn(borrowed,report['cases'][0]['joinedRunIds'])

    def test_client_terminal_outcome_is_classified_without_duplicate_backend_errors(self):
        batch,ids=self.cohort();ctx={'batchId':batch['id'],'caseId':ids[0]}
        token=B.begin(ctx,'discover_project_components',{})
        B.finish(token,{'ok':True,'root':'/not/a/fixture','components':[{'requiresContainer':True}]})
        B.operation({'action':'client_outcome','context':ctx,'code':'container_required'})
        report=B.export_batch(batch['id'],refresh=False)
        self.assertEqual(report['cases'][0]['outcome'],'unsupported')
        self.assertIn('container',json.dumps(report['cases'][0]))
        size=len(report['cases'][0]['events'])
        B.operation({'action':'client_outcome','context':ctx,'code':'controller_error'})
        self.assertEqual(len(B.export_batch(batch['id'],refresh=False)['cases'][0]['events']),size)

    def test_short_public_values_preserve_schema_and_checkout_capture(self):
        batch,ids=self.cohort();ctx={'batchId':batch['id'],'caseId':ids[0]}
        token=B.begin(ctx,'save_project_environment',{'values':{'NAME':'a','PORT':'1','SECRET':'nested-private-key'}})
        B.finish(token,{'ok':True,'publicValues':{'NAME':'a','PORT':'1'}})
        with mock.patch.object(B,'checkout_revision',return_value={'actualCommit':'c'*40}):
            token=B.begin(ctx,'discover_project_components',{})
            B.finish(token,{'ok':True,'root':'/fixture','components':[],'stdout':'name=a, port=1','trace':{'nested-private-key':'value'}})
        report=B.export_batch(batch['id'],refresh=False);case=report['cases'][0]
        self.assertEqual(case['checkoutRevision']['actualCommit'],'c'*40)
        self.assertEqual(case['events'][-1]['operation'],'discover_project_components')
        self.assertEqual(case['events'][-1]['stage'],'checkout_discovery')
        self.assertEqual(case['events'][-1]['status'],'done')
        self.assertIn('[REDACTED]',case['events'][-1]['response']['stdout'])
        self.assertNotIn('nested-private-key',json.dumps(case['events']))

    def test_order_failure_survives_poll_eviction_and_fixed_rate(self):
        batch,ids=self.cohort();ctx={'batchId':batch['id'],'caseId':ids[0]}
        for _ in range(25):
            token=B.begin(ctx,'project_order_state',{})
            B.finish(token,{'ok':True,'order':{'status':'assessing'}})
        token=B.begin(ctx,'project_order_state',{})
        B.finish(token,{'ok':True,'order':{'status':'error','error':'Specific assessment boundary failed','stage':'assessment','agentTrace':[{'error':'model unavailable'}]}})
        for _ in range(50):
            token=B.begin(ctx,'project_run_state',{})
            B.finish(token,{'ok':True,'run':{'status':'running','healthy':True}})
        report=B.export_batch(batch['id'],refresh=False)
        first=report['cases'][0]['firstFailure']
        self.assertEqual(first['error'],'Specific assessment boundary failed')
        self.assertIn('model unavailable',json.dumps(first))
        self.assertNotIn('Specific assessment boundary failed',json.dumps(report['cases'][0]['events']))
        self.assertEqual(report['summary']['healthyStartupRate'],0.2)

class BenchmarkBoundaryTests(unittest.TestCase):
    def test_http_recording_local_scope_and_secret_redaction(self):
        from test_goal_page import server_for,post_json
        from human_compact.trajectory import project_environment as PE
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':tmp,'HC_AUTOSYNC_SECONDS':'0'}):
            chat=Path(tmp)/'chat';chat.mkdir()
            with server_for(chat) as url:
                d=post_json(url+'/api/op',{'op':'project_benchmark','action':'import','csv':dataset([row(1)]),'name':'local.csv'})['dataset']
                b=post_json(url+'/api/op',{'op':'project_benchmark','action':'create','datasetId':d['id'],'selectedIds':[d['cases'][0]['id']]})['batch']
                ctx={'batchId':b['id'],'caseId':b['cases'][0]['id']}
                with mock.patch.object(PE,'save',return_value={'ok':True,'publicValues':{'NAME':'private-http-name'},'trace':{'prompt':'private-http-name'}}):
                    post_json(url+'/api/op',{'op':'save_project_environment','path':str(chat),'values':{'NAME':'private-http-name'},'benchmark':ctx})
                report=post_json(url+'/api/op',{'op':'project_benchmark','action':'export','id':b['id']})['batch']
                self.assertNotIn('private-http-name',json.dumps(report))
                self.assertIn('environment',json.dumps(report))
            with server_for(chat,chat_scoped=False) as url:
                self.assertFalse(post_json(url+'/api/op',{'op':'project_benchmark','action':'latest'})['ok'])

    def test_frontend_cohort_selection_and_restore(self):
        import shutil
        import subprocess
        source=Path(__file__).resolve().parents[1]/'hc/src/human_compact/trajectory/web/goal'
        with tempfile.TemporaryDirectory() as tmp:
            modules=Path(tmp)/'goal';shutil.copytree(source,modules)
            (modules/'package.json').write_text('{"type":"module"}')
            (modules/'test.mjs').write_text(Path(__file__).with_name('project_benchmark_ui.mjs').read_text())
            run=subprocess.run(['node',str(modules/'test.mjs')],capture_output=True,text=True)
            self.assertEqual(0,run.returncode,run.stdout+run.stderr)

class BenchmarkExecutionTests(unittest.TestCase):
    setUp=BenchmarkTests.setUp
    cohort=BenchmarkTests.cohort
    def test_real_runner_evidence_preserves_failed_attempt_on_retry(self):
        import shlex
        import sys
        import time
        from human_compact.trajectory import project_run as R
        repo=self.home/'fixture';repo.mkdir()
        gate=repo/'allow-start'
        fail=f"from pathlib import Path; import sys; print('fixture install'); sys.exit(0 if Path({str(gate)!r}).exists() else 7)"
        command=lambda code:shlex.join([sys.executable,'-u','-c',code])
        rid=R.retain({'path':str(repo),'plan':{'steps':[{'name':'install','commands':[{'cmd':command(fail)}]}],'deploy':{'startCommand':command("import http.server; s=http.server.HTTPServer(('127.0.0.1',0),http.server.SimpleHTTPRequestHandler); print('http://127.0.0.1:'+str(s.server_port)); s.serve_forever()")}}})
        b,ids=self.cohort();ctx={'batchId':b['id'],'caseId':ids[0]}
        def wait(status):
            until=time.monotonic()+12
            while time.monotonic()<until:
                result=R.view(rid)
                if result['status'] in status:return result
                time.sleep(.05)
            self.fail(str(result))
        try:
            with mock.patch.object(R,'recover',side_effect=lambda id,job,redact:job['state'].update(status='failed')):
                R.start(rid);failed=wait({'failed'})
                token=B.begin(ctx,'project_run_state',{});B.finish(token,{'ok':True,'run':failed})
                gate.touch();R.start(rid,retry=True);healthy=wait({'running','failed'})
                self.assertTrue(healthy.get('healthy'),healthy)
                token=B.begin(ctx,'project_run_state',{});B.finish(token,{'ok':True,'run':healthy})
            report=B.export_batch(b['id'])
            case=report['cases'][0]
            self.assertEqual(case['outcome'],'healthy_startup')
            self.assertIn('fixture install',json.dumps(case));self.assertIn('"exitCode": 7',json.dumps(case))
            self.assertTrue(any(e.get('durationSeconds') is not None for e in case['events']))
            # A second case sharing this checkout receives the controller's real
            # joinedExistingRun response. It must not borrow the first case's pass.
            second=R.retain({'path':str(repo),'plan':R.read(rid)['plan']})
            other={'batchId':b['id'],'caseId':ids[1]}
            token=B.begin(other,'project_run_start',{'id':second})
            joined=R.start(second)
            self.assertTrue(joined['joinedExistingRun'])
            B.finish(token,{'ok':True,'run':joined})
            token=B.begin(other,'project_run_state',{'id':rid})
            B.finish(token,{'ok':True,'run':R.view(rid)})
            report=B.export_batch(b['id'])
            self.assertEqual(report['summary']['healthyStartup'],1)
            self.assertEqual(report['cases'][1]['outcome'],'blocked')
            self.assertEqual(report['cases'][1]['runId'],second)

        finally:R.reset(rid);R._JOBS.pop(rid,None)

    def test_checkout_revision_and_global_report_budget(self):
        import subprocess
        repo=self.home/'git';repo.mkdir()
        subprocess.run(['git','init',str(repo)],check=True,capture_output=True)
        subprocess.run(['git','-C',str(repo),'-c','user.name=Fixture','-c','user.email=fixture@example.test','commit','--allow-empty','-m','fixture'],check=True,capture_output=True)
        sha=subprocess.run(['git','-C',str(repo),'rev-parse','HEAD'],check=True,capture_output=True,text=True).stdout.strip()
        b,ids=self.cohort();ctx={'batchId':b['id'],'caseId':ids[0]}
        token=B.begin(ctx,'discover_project_components',{})
        B.finish(token,{'ok':True,'root':str(repo),'components':[]})
        case=B.export_batch(b['id'],refresh=False)['cases'][0]
        self.assertEqual(case['checkoutRevision']['actualCommit'],sha)
        self.assertIsNone(case['checkoutRevision']['matchesReviewedCommit'])
        with mock.patch.object(B,'MAX_BATCH_BYTES',60000):
            for cid in ids:
                for i in range(8):
                    token=B.begin({'batchId':b['id'],'caseId':cid},'project_run_state',{})
                    B.finish(token,{'ok':False,'error':str(i)+'x'*12000})
            raw=(B.folder()/(b['id']+'.json')).read_bytes()
            self.assertLessEqual(len(raw),60000)
            report=B.export_batch(b['id'],refresh=False)
            self.assertEqual(len(report['cases']),5);self.assertIn('truncat',json.dumps(report).lower())

class BenchmarkSecurityTests(unittest.TestCase):
    def test_new_operations_reject_cross_origin_host_and_shared_workspace(self):
        import urllib.error
        from test_goal_page import server_for,post_json
        from human_compact.trajectory import ui
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':tmp,'HC_AUTOSYNC_SECONDS':'0'}):
            chat=Path(tmp)/'chat';chat.mkdir()
            with server_for(chat) as url:
                for headers in ({'Origin':'https://untrusted.example'},{'Host':'attacker.example'}):
                    with self.assertRaises(urllib.error.HTTPError) as result:
                        post_json(url+'/api/op',{'op':'project_benchmark','action':'latest'},headers)
                    self.assertEqual(result.exception.code,403)
            configure=ui._configure_server
            def shared(server,*args,**kw):
                configure(server,*args,**kw);server.shared_project='fixture-shared'
            with mock.patch.object(ui,'_configure_server',side_effect=shared),server_for(chat) as url:
                answer=post_json(url+'/api/op',{'op':'project_benchmark','action':'import','csv':dataset([row(1)])})
                self.assertFalse(answer['ok']);self.assertIn('shared workspace',answer['error'])
            self.assertFalse((Path(tmp)/'project-benchmarks').exists())

    def test_secret_is_redacted_after_server_memory_reset(self):
        from human_compact.trajectory import project_environment as PE
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':tmp}):
            repo=Path(tmp)/'repo';repo.mkdir();(repo/'.env.example').write_text('API_KEY=\n')
            PE.save(str(repo),{'API_KEY':'persisted-private-value'})
            B._SECRETS.clear()
            data=B.import_csv(dataset([row(1)]));batch=B.create_batch(data['id'],[data['cases'][0]['id']])
            token=B.begin({'batchId':batch['id'],'caseId':batch['cases'][0]['id']},'analyze_project',{})
            B.finish(token,{'ok':False,'error':'persisted-private-value'})
            report=B.export_batch(batch['id'],refresh=False)
            self.assertNotIn('persisted-private-value',json.dumps(report))
            self.assertNotIn('persisted-private-value',(B.folder()/(batch['id']+'.json')).read_text())
