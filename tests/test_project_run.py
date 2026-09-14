import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import time
import unittest
from unittest import mock
from human_compact.trajectory import project_run as R, preview as PV

class RunTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.repo=self.root/'repo';self.repo.mkdir()
        self.patch=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(self.root/'hc')});self.patch.start();self.addCleanup(self.patch.stop)
        self.addCleanup(self.stop)
        def no_recovery(id,job,redact):job['state']['status']='failed'
        patch=mock.patch.object(R,'recover',side_effect=no_recovery);patch.start();self.addCleanup(patch.stop)
    def stop(self):
        proc=PV.running(str(self.repo))
        if proc:proc.stop()
        PV.forget(str(self.repo))
        R._JOBS.clear()
    def py(self,code):return shlex.quote(sys.executable)+' -u -c '+shlex.quote(code)
    def plan(self,install=None,build=None,start=None):
        result={'path':str(self.repo),'plan':{'steps':[
            {'name':'install','commands':[{'cmd':install or self.py("print('installed')")}]},
            {'name':'build','commands':[{'cmd':build or self.py("print('built')")}]}],
            'deploy':{'startCommand':start or self.py("import http.server; s=http.server.HTTPServer(('127.0.0.1',0),http.server.SimpleHTTPRequestHandler); print('http://127.0.0.1:'+str(s.server_port)); s.serve_forever()")}}}
        return R.retain(result)
    def wait(self,id,status):
        end=time.monotonic()+12
        while time.monotonic()<end:
            r=R.view(id)
            if r['status'] in status:return r
            time.sleep(.08)
        self.fail(str(r))
    def test_assessed_plan_worker_import_failure_is_persisted(self):
        id=R.retain({'path':str(self.repo),'plan':{},'nativePlan':{'status':'plan'}})
        with mock.patch.object(R,'recover',side_effect=ModuleNotFoundError("No module named 'packaging'")):
            R.start(id)
            state=self.wait(id,{'failed'})
        self.assertTrue(state['workerError'])
        self.assertIn('packaging',state['reason'])
        self.assertFalse(state['healthy'])
        self.assertEqual('failed',R.read(id)['run']['status'])

    def test_approved_worker_failure_is_persisted(self):
        id=self.plan()
        record=R.read(id)
        record['run']={'id':id,'cwd':str(self.repo),'status':'awaiting_approval',
                       'approval':{'id':'approval','status':'pending','proposal':{}},'stages':[]}
        R.write(id,record)
        with mock.patch.object(R,'recover',side_effect=RuntimeError('Unexpected host error')):
            R.decide_approval(id,'approval',True)
            state=self.wait(id,{'failed'})
        self.assertTrue(state['workerError'])
        self.assertIn('Unexpected host error',R.read(id)['run']['reason'])

    def test_success_sequential_persistent_http_and_logs(self):
        id=self.plan();initial=R.start(id);self.assertNotEqual('running',initial['status'])
        r=self.wait(id,{'running','failed'});self.assertEqual('running',r['status'],r)
        self.assertTrue(r['healthy']);self.assertTrue(PV.running(str(self.repo)).alive())
        self.assertEqual(['done','done','done'],[s['status'] for s in r['stages']])
        self.assertIn('installed',r['stages'][0]['stdout']);self.assertIn('built',r['stages'][1]['stdout'])
        time.sleep(.2);self.assertTrue(PV.running(str(self.repo)).alive())
        self.assertEqual(r['pid'],R.start(id)['pid'])
        self.assertTrue(R.reset(id)['ok'])
        self.assertFalse(PV.running(str(self.repo)).alive())
        self.assertEqual('Stopped by Reset',R.view(id)['reason'])
    def test_failures_stop_without_repair_and_retry_same_command(self):
        for failed in ('install','build'):
            self.stop()
            command=self.py("import sys; print('actual failure',file=sys.stderr); sys.exit(7)")
            id=self.plan(**{failed:command});R.start(id)
            r=self.wait(id,{'failed'})
            self.assertEqual(failed,r['stage']);self.assertEqual(7,r['exitCode'])
            self.assertEqual(command,r['command']);self.assertIn('actual failure',r['stderr'])
            self.assertEqual('pending',r['stages'][-1]['status'])
            R.start(id,retry=True);retry=self.wait(id,{'failed'})
            self.assertEqual(command,retry['command']);self.assertEqual(7,retry['exitCode'])
    def test_start_exit_and_start_timeout(self):
        id=self.plan(start=self.py("import sys; print('no server',file=sys.stderr); sys.exit(3)"));R.start(id)
        r=self.wait(id,{'failed'});self.assertEqual('start',r['stage']);self.assertIn('before a healthy',r['reason'])
        self.stop()
        id=self.plan(start=self.py('import time; time.sleep(30)'))
        with mock.patch.object(R,'START_TIMEOUT',.2):
            R.start(id);r=self.wait(id,{'failed'})
        self.assertIn('timeout',r['reason'])
    def test_environment_redaction_and_no_inherited_hc_secret(self):
        (self.repo/'.env.local').write_text('TOKEN=secret-123456\n')
        command=self.py("import os,sys; assert os.environ['TOKEN']=='secret-123456'; assert 'UNRELATED_API_KEY' not in os.environ; print(os.environ['TOKEN']); print(os.environ['TOKEN'],file=sys.stderr); sys.exit(5)")
        id=self.plan(install=command)
        with mock.patch.dict(os.environ,{'UNRELATED_API_KEY':'must-not-pass'}):R.start(id)
        r=self.wait(id,{'failed'})
        self.assertEqual(5,r['exitCode']);self.assertNotIn('secret-123456',json.dumps(r));self.assertIn('[redacted]',r['stdout'])
    def test_no_model_entrypoints_and_real_http_start_returns(self):
        from test_goal_page import server_for,post_json
        chat=self.root/'chat';chat.mkdir();id=self.plan()
        with mock.patch.object(PV,'_ask_model',side_effect=AssertionError('model forbidden')), mock.patch.object(PV,'recover',side_effect=AssertionError('repair forbidden')):
            with server_for(chat) as url:
                response=post_json(url+'/api/op',{'op':'project_run_start','id':id})
                self.assertTrue(response['ok'])
                r=self.wait(id,{'running','failed'})
                self.assertEqual('running',r['status'])
                self.assertTrue(PV.running(str(self.repo)).alive())

class EnvironmentSkipTests(unittest.TestCase):
    def test_only_explicit_names_are_skipped_without_inventing_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            report={'framework':'vite','variables':[{'name':'API_URL','status':'missing'},{'name':'TOKEN','status':'missing'}]}
            with mock.patch.object(R.PE,'project',return_value=root), mock.patch.object(R.PE,'scan',return_value=report), mock.patch.object(R.PE,'saved',return_value={}):
                with self.assertRaises(ValueError):R.environment(directory,skipped=['API_URL'])
                base,values,_=R.environment(directory,skipped=['API_URL','TOKEN'])
                self.assertNotIn('API_URL',base)
                self.assertNotIn('TOKEN',values)
                with self.assertRaises(ValueError):R.environment(directory)
