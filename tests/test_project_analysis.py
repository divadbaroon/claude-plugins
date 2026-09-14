"""Railpack is mocked at Popen; no generated command is ever executed."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'hc/src'))
from human_compact.trajectory import project_analysis as PA

class AnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patch=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(Path(self.temp.name)/'hc')});patch.start();self.addCleanup(patch.stop)
        self.repo = Path(self.temp.name).resolve() / 'project with spaces'
        self.repo.mkdir()
        (self.repo/'package.json').write_text('{"scripts":{"install":"touch SHOULD_NOT_EXIST"}}')
        self.which = mock.patch.object(PA.shutil, 'which', return_value='/tools/railpack').start()
        self.addCleanup(mock.patch.stopall)

    def process(self, command, **kw):
        self.assertEqual(command[:2], ['/tools/railpack', 'prepare'])
        self.assertEqual(command[-1], str(self.repo))
        self.assertNotEqual(kw['cwd'], str(self.repo))
        self.assertNotIn('shell', kw)
        self.assertEqual(kw['stdin'], subprocess.DEVNULL)
        Path(command[3]).write_text(json.dumps({'steps':[{'name':'install','commands':[{'cmd':'touch SHOULD_NOT_EXIST'}]}]}))
        Path(command[5]).write_text('{"success":true,"detectedProviders":["node"]}')
        kw['stdout'].write(b'Detected Node\ninstall: touch SHOULD_NOT_EXIST\n')
        return mock.Mock(returncode=0, poll=mock.Mock(return_value=0))

    def test_success_raw_plan_unchanged_repository_one_prepare_only(self):
        before={p.name:p.read_bytes() for p in self.repo.iterdir()}
        with mock.patch.object(PA.subprocess, 'Popen', side_effect=self.process) as run:
            result=PA.analyze(str(self.repo))
        self.assertTrue(result['ok'],result)
        self.assertIn('touch SHOULD_NOT_EXIST',result['rawPlan'])
        self.assertEqual(['node'],result['info']['detectedProviders'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in self.repo.iterdir()})
        self.assertEqual(1,run.call_count)
        self.assertFalse(Path(run.call_args.args[0][3]).exists())

    def test_invalid_paths_never_launch(self):
        with mock.patch.object(PA.subprocess,'Popen') as run:
            for path in ('',None,'relative',str(self.repo/'missing'),str(self.repo/'package.json')):
                self.assertFalse(PA.analyze(path)['ok'])
            run.assert_not_called()

    def test_unavailable(self):
        self.which.return_value=None
        self.assertIn('not installed',PA.analyze(str(self.repo))['error'])

    def test_failure_keeps_actual_output_and_allows_retry(self):
        def fail(command,**kw):
            kw['stderr'].write(b'No provider supports these files')
            return mock.Mock(returncode=1,poll=mock.Mock(return_value=1))
        with mock.patch.object(PA.subprocess,'Popen',side_effect=fail):
            result=PA.analyze(str(self.repo))
        self.assertFalse(result['ok']);self.assertIn('No provider',result['error'])
        with mock.patch.object(PA.subprocess,'Popen',side_effect=self.process):
            self.assertTrue(PA.analyze(str(self.repo))['ok'])

    def test_timeout_kills_and_reaps_process(self):
        proc=mock.Mock(returncode=-9,poll=mock.Mock(return_value=None))
        with mock.patch.object(PA.subprocess,'Popen',return_value=proc), mock.patch.object(PA,'TIMEOUT',0):
            self.assertIn('timed out',PA.analyze(str(self.repo))['error'])
        proc.kill.assert_called_once();proc.wait.assert_called_once()

    def test_output_limit(self):
        def large(command,**kw):
            kw['stdout'].write(b'x'*(PA.LIMIT+1))
            return mock.Mock(returncode=0,poll=mock.Mock(return_value=0))
        with mock.patch.object(PA.subprocess,'Popen',side_effect=large):
            self.assertIn('limit',PA.analyze(str(self.repo))['error'])

    def test_concurrent_projects_and_same_project_replacement(self):
        from concurrent.futures import ThreadPoolExecutor
        import threading
        started=threading.Event()
        class Slow:
            returncode=None
            def poll(self):return self.returncode
            def kill(self):self.returncode=-9
            def wait(self):return self.returncode
        slow=Slow();calls=[]
        other=self.repo.parent/'other';other.mkdir();(other/'package.json').write_text('{}')
        def launch(command,**kw):
            calls.append(command[-1])
            if len(calls)==1:started.set();return slow
            Path(command[3]).write_text('{"steps":[]}');Path(command[5]).write_text('{}')
            return mock.Mock(returncode=0,poll=mock.Mock(return_value=0))
        with mock.patch.object(PA.subprocess,'Popen',side_effect=launch),ThreadPoolExecutor(3) as pool:
            old=pool.submit(PA.analyze,str(self.repo))
            self.assertTrue(started.wait(2))
            different=pool.submit(PA.analyze,str(other)).result(timeout=2)
            self.assertTrue(different['ok'])
            self.assertIsNone(slow.returncode,'different project must not cancel the first')
            replacement=pool.submit(PA.analyze,str(self.repo)).result(timeout=3)
            self.assertTrue(replacement['ok'])
            self.assertTrue(old.result(timeout=2)['cancelled'])
            self.assertEqual(slow.returncode,-9)
        self.assertFalse(PA._ACTIVE)

    def test_frontend_modal_lifecycle_and_local_service_boundary(self):
        source=Path(__file__).resolve().parents[1]/'hc/src/human_compact/trajectory/web/goal'
        modules=Path(self.temp.name)/'goal'
        shutil.copytree(source,modules)
        (modules/'package.json').write_text('{"type":"module"}')
        script=Path(__file__).with_name('project_analysis_ui.mjs').read_text()
        (modules/'test.mjs').write_text(script)
        run=subprocess.run(['node',str(modules/'test.mjs')],capture_output=True,text=True)
        self.assertEqual(0,run.returncode,run.stdout+run.stderr)

class LocalRouteTests(unittest.TestCase):
    def test_loopback_route_runs_analysis_outside_goal_operations(self):
        from test_goal_page import server_for, post_json
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':temp,'HC_AUTOSYNC_SECONDS':'0'}):
            chat=Path(temp)/'chat';chat.mkdir()
            with server_for(chat) as url, mock.patch.object(PA,'analyze',return_value={'ok':True,'rawPlan':'{}'}) as analyze:
                response=post_json(url+'/api/op',{'op':'analyze_project','path':'/local/project'})
                self.assertEqual('{}',response['rawPlan'])
                analyze.assert_called_once_with('/local/project',None)
                self.assertFalse((chat/'goals.json').exists())
            with server_for(chat,chat_scoped=False) as url, mock.patch.object(PA,'analyze') as analyze:
                self.assertFalse(post_json(url+'/api/op',{'op':'analyze_project','path':'/local/project'})['ok'])
                analyze.assert_not_called()
