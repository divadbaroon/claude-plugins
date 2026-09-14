import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from human_compact.trajectory import project_setup as S, project_run as R, project_health as H, preview as PV

class LaunchRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'repo';self.root.mkdir()
        env=patch.dict(os.environ,{'HUMAN_COMPACT_HOME':self.temp.name+'/home'});env.start();self.addCleanup(env.stop)
        self.addCleanup(self.stop)
    def stop(self):
        for id in list(R._JOBS):R.reset(id)
        R._JOBS.clear()
    def test_managed_process_suppresses_browser_without_mutating_environment(self):
        env={**os.environ,'BROWSER':'open'}
        proc=PV.start_plan_process(str(self.root),'browser fixture',env,str,
            argv=[sys.executable,'-c','import os; print(os.environ["BROWSER"])'])
        proc.process.wait(timeout=5);proc.thread.join(timeout=3)
        self.assertEqual('none',proc.logs()['stdout'].strip())
        self.assertEqual('open',env['BROWSER'])

    def test_vite_options_are_narrowly_validated(self):
        (self.root/'package.json').write_text(json.dumps({'scripts':{'dev':'vite --config vite.config.ts','other':'node server.js'}}))
        step={'cwd':'.','argv':['npm','run','dev','--','--host','127.0.0.1','--port','5173','--strictPort']}
        self.assertEqual(step['argv'],S.command(self.root,step,False)['argv'])
        for args in [['--host','0.0.0.0'],['--port','80'],['--config','evil.js'],['--host','::1','--host','127.0.0.1']]:
            with self.subTest(args=args),self.assertRaises(ValueError):S.command(self.root,{**step,'argv':['npm','run','dev','--',*args]},False)
        with self.assertRaises(ValueError):S.command(self.root,{**step,'argv':['npm','run','other','--','--host','127.0.0.1']},False)
    def test_ipv6_owned_listener_and_unrelated_listener(self):
        if not socket.has_ipv6:self.skipTest('No IPv6')
        with socket.socket(socket.AF_INET6) as sock:
            try:sock.bind(('::1',0))
            except OSError:self.skipTest('IPv6 loopback unavailable')
            port=sock.getsockname()[1]
        code="import socket,http.server\nclass Server(http.server.HTTPServer):address_family=socket.AF_INET6\nServer(('::1',%d),http.server.SimpleHTTPRequestHandler).serve_forever()"%port
        proc=PV.start_plan_process(str(self.root),'fixture',dict(os.environ),str,owner='health-fixture',argv=[sys.executable,'-c',code])
        try:
            end=time.monotonic()+8;actual=None
            while time.monotonic()<end and not actual:actual=H.probe(proc,f'http://127.0.0.1:{port}/');time.sleep(.05)
            self.assertEqual(f'http://[::1]:{port}/',actual)
            other=self.root/'other';other.mkdir()
            sleeper=PV.start_plan_process(str(other),'sleeper',dict(os.environ),str,owner='unrelated-fixture',argv=[sys.executable,'-c','import time;time.sleep(10)'])
            try:self.assertIsNone(H.probe(sleeper,f'http://[::1]:{port}/'))
            finally:sleeper.stop()
        finally:proc.stop()
    def test_rejected_proposal_is_corrected_without_execution(self):
        (self.root/'index.html').write_text('fixture')
        from human_compact.trajectory import project_static as Static
        good=Static.plan(self.root,self.root)
        bad={**good,'services':[{**good['services'][0],'kind':'unknown'}]}
        id=R.retain({'path':str(self.root),'plan':{},'nativePlan':bad})
        with patch.object(S,'propose',return_value=good) as propose:
            R.start(id)
            end=time.monotonic()+12
            while time.monotonic()<end:
                result=R.view(id)
                if result['status'] in ('running','failed'):break
                time.sleep(.05)
            self.assertEqual('running',result['status'],result)
            self.assertEqual(1,propose.call_count)
            evidence=propose.call_args.args[1]
            self.assertEqual('validation',evidence['stage'])
            self.assertIn('Unsupported service kind',evidence['validationError'])
            self.assertEqual('rejected',result['attempts'][0]['status'])

    def test_validation_repair_budget_and_repeat_detection(self):
        from human_compact.trajectory import project_static as Static
        (self.root/'index.html').write_text('fixture')
        base=Static.plan(self.root,self.root)
        def bad(n):return {**base,'services':[{**base['services'][0],'kind':'unknown','argv':['invalid-'+str(n)]}]}
        for repeated in (True,False):
            with self.subTest(repeated=repeated):
                id=R.retain({'path':str(self.root),'plan':{},'nativePlan':bad(0)})
                proposals=[bad(0)] if repeated else [bad(n) for n in range(1,6)]
                with patch.object(S,'propose',side_effect=proposals) as propose:
                    R.start(id);end=time.monotonic()+5
                    while time.monotonic()<end:
                        state=R.view(id)
                        if state['status']=='failed':break
                        time.sleep(.02)
                    self.assertEqual('failed',state['status'])
                    self.assertEqual(1 if repeated else 5,propose.call_count)
                    self.assertEqual([],state['stages'])
                    self.assertIn('repeated' if repeated else '5 repair attempts',state['reason'])
                R.reset(id)
