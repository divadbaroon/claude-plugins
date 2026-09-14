import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest import mock
from human_compact.trajectory import project_setup as S, project_run as R, preview as PV

class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)/'repo';self.root.mkdir()
        self.env=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(Path(self.temp.name)/'hc')});self.env.start();self.addCleanup(self.env.stop)
        self.addCleanup(self.stop)
        (self.root/'package.json').write_text(json.dumps({'scripts':{'start':'node server.js','build':'build-tool'}}))
        (self.root/'README.md').write_text('Launch the local server. TOKEN=secret-value\n')
    def stop(self):
        for id in list(R._JOBS):R.reset(id)
        time.sleep(.2)
        R._JOBS.clear()
    def port(self):
        with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
    def plan(self,fail=False):
        services=[]
        for name in ('backend','frontend'):
            port=self.port()
            (self.root/(name+'.py')).write_text('raise SystemExit(9)' if fail else "import http.server\ns=http.server.HTTPServer(('127.0.0.1',%d),http.server.SimpleHTTPRequestHandler)\ns.serve_forever()\n"%port)
            services.append({'id':name,'cwd':'.','argv':['python3',name+'.py'],'dependsOn':[] if name=='backend' else ['backend'],'healthUrl':'http://127.0.0.1:'+str(port)})
        return {'status':'plan','summary':'Use documented services','preparation':[],'services':services,'entryService':'frontend'}
    def retained(self):
        return R.retain({'path':str(self.root),'plan':{'steps':[{'name':'install','commands':[{'cmd':'/app/.venv/bin/pip install -r requirements.txt'}]}],'deploy':{'startCommand':'unused'}}})
    def wait(self,id):
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            result=R.view(id)
            if result['status'] in ('running','failed','needs_input','unsupported'):return result
            time.sleep(.04)
        self.fail(str(result))
    def test_multiservice_success_join_reload_and_reset(self):
        plan=self.plan();id=self.retained()
        with mock.patch.object(S,'propose',return_value=plan) as propose:
            self.assertEqual('ready',R.view(id)['status']);propose.assert_not_called()
            R.start(id);R.start(id)
            result=self.wait(id)
            self.assertEqual('running',result['status'],result)
            self.assertEqual(1,propose.call_count)
            self.assertEqual(2,len(R._JOBS[id]['setupProcs']))
            self.assertTrue(PV.running(str(self.root)).alive())
            with self.assertRaises(ValueError):PV.start_plan_process(str(self.root),'must-not-run',{},str)
            self.assertTrue(all(p.alive() for p in R._JOBS[id]['setupProcs'].values()))
            self.assertEqual(result['url'],plan['services'][1]['healthUrl'])
            self.assertEqual(1,len(R.read(id)['run']['attempts']))
            self.assertEqual(result['pid'],R.start(id)['pid'])
            R.reset(id)
            self.assertTrue(all(not p.alive() for p in R._JOBS[id]['setupProcs'].values()))
    def test_service_ids_preserve_namespace_and_reject_malformed_or_duplicates(self):
        plan=self.plan()
        plan['services'][0]['id']='system/backend'
        plan['services'][1].update(id='system/frontend',dependsOn=['system/backend'])
        plan['entryService']='system/frontend'
        result=S.validate(self.root,plan)
        self.assertEqual('system/frontend',result['entryService'])
        self.assertEqual(['system/backend'],result['services'][1]['dependsOn'])
        for invalid in ('system/backend', '../backend', '/backend', 'system//frontend',
                        'system/frontend/', 'system:frontend', 'a'*161, 'system/../frontend'):
            broken=json.loads(json.dumps(plan));broken['services'][1]['id']=invalid
            with self.subTest(id=invalid),self.assertRaisesRegex(ValueError,'Invalid or duplicate'):
                S.validate(self.root,broken)

    def repair_sequence(self, count):
        base=self.plan();plans=[]
        for n in range(count):
            plan=json.loads(json.dumps(base))
            entry='failed_backend_'+str(n)+'.py'
            (self.root/entry).write_text('raise SystemExit(9)')
            plan['services'][0]['argv']=['python3',entry]
            plans.append(plan)
        return plans,base

    def test_failed_service_bounded_repair(self):
        plans,_=self.repair_sequence(5)
        id=self.retained()
        with mock.patch.object(S,'propose',side_effect=plans+[AssertionError('Sixth attempt must not run')]) as propose:
            R.start(id);result=self.wait(id)
            self.assertEqual('failed',result['status'],result)
            self.assertEqual(5,propose.call_count)
            self.assertEqual(5,len(result['attempts']))
            self.assertIn('5 repair attempts',result['reason'])
            self.assertEqual(9,propose.call_args.args[1]['exitCode'])
            self.assertEqual(9,propose.call_args.args[1]['originalFailure']['exitCode'])
            self.assertFalse(any(p.alive() for p in R._JOBS[id]['setupProcs'].values()))

    def test_fifth_repair_can_reach_healthy_services(self):
        plans,success=self.repair_sequence(4)
        with mock.patch.object(S,'propose',side_effect=plans+[success]) as propose:
            id=self.retained();R.start(id);result=self.wait(id)
            self.assertEqual('running',result['status'],result)
            self.assertEqual(5,propose.call_count)
            self.assertEqual([1,2,3,4,5],[a['number'] for a in result['attempts']])
            R.reset(id)

    def test_needs_input_and_rejected_commands_never_execute(self):
        for proposal in ({'status':'needs_input','reason':'Supply API key in Environment check'},
                         {**self.plan(),'preparation':[{'id':'cleanup','cwd':'.','argv':['rm','-rf','.']}]},
                         {**self.plan(),'preparation':[{'id':'escape','cwd':'..','argv':['npm','install']}]}):
            id=self.retained()
            with mock.patch.object(S,'propose',return_value=proposal),mock.patch.object(PV,'start_plan_process') as launch:
                R.start(id);result=self.wait(id)
                self.assertIn(result['status'],('failed','needs_input'))
                launch.assert_not_called()
    def test_deterministic_failure_triggers_compact_recovery(self):
        id=R.retain({'path':str(self.root),'plan':{'steps':[{'name':'install','commands':[{'cmd':'exit 7'}]}],'deploy':{'startCommand':'must-not-run'}}})
        with mock.patch.object(S,'propose',return_value={'status':'needs_input','reason':'Need a supported runtime'}) as propose:
            R.start(id);result=self.wait(id)
            self.assertEqual('needs_input',result['status'])
            failure=propose.call_args.args[1]
            self.assertEqual('install',failure['stage']);self.assertEqual('exit 7',failure['command']);self.assertEqual(7,failure['exitCode'])
            self.assertEqual('pending',result['stages'][1]['status'])
            self.assertEqual('exit 7',R.read(id)['run']['failures'][0]['command'])
    def test_timeout_stops_services_and_unavailable_provider_is_terminal(self):
        plan=self.plan()
        (self.root/'backend.py').write_text('import time; time.sleep(30)')
        id=self.retained()
        with mock.patch.object(S,'propose',side_effect=[plan,{'status':'unsupported','reason':'No healthy endpoint'}]),mock.patch.object(R,'START_TIMEOUT',.1):
            R.start(id);result=self.wait(id)
            self.assertEqual('unsupported',result['status'],result)
            self.assertIn('timed out',result['attempts'][0]['reason'])
            self.assertFalse(any(p.alive() for p in R._JOBS[id]['setupProcs'].values()))
        id=self.retained()
        with mock.patch.object(S,'propose',side_effect=RuntimeError('claude CLI unavailable')) as propose:
            R.start(id);result=self.wait(id)
            self.assertEqual('failed',result['status']);self.assertIn('unavailable',result['reason']);self.assertEqual(1,propose.call_count)

    def test_validation_cycles_remote_health_and_symlinks(self):
        for mutation in ('cycle','url','symlink'):
            plan=self.plan()
            if mutation=='cycle':plan['services'][0]['dependsOn']=['frontend']
            elif mutation=='url':plan['services'][0]['healthUrl']='http://example.com:8080'
            else:
                link=self.root/'escape';link.symlink_to(self.root.parent,target_is_directory=True)
                plan['services'][0]['cwd']='escape'
            with self.assertRaises(ValueError):S.validate(self.root,plan)
    def test_compact_tool_free_brief_requested_files_and_secret_redaction(self):
        (self.root/'.env.local').write_text('TOKEN=secret-value\n')
        (self.root/'sub').mkdir();(self.root/'sub'/'requirements.txt').write_text('flask==3.0\n')
        engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps({'status':'read_more','files':['sub/requirements.txt']}),json.dumps({'status':'needs_input','reason':'Missing setup detail'})]
        _,_,redact=R.environment(str(self.root))
        with mock.patch.object(PV,'_engine',return_value=engine):
            result=S.propose({'cwd':str(self.root)}, {'stage':'install','stderr':'secret-value '+('x'*100000)}, [],redact)
        self.assertEqual('needs_input',result['status'])
        self.assertEqual(2,engine.generate_plain.call_count)
        for call in engine.generate_plain.call_args_list:
            prompt=call.args[0];self.assertLessEqual(len(prompt),S.MAX_PROMPT);self.assertNotIn('secret-value',prompt)
        self.assertIn('flask==3.0',engine.generate_plain.call_args.args[0])
        engine.generate_searching.assert_not_called()
        with self.assertRaises(ValueError):S.excerpt(self.root,'.env.local',redact)
    def test_bun_install_approval_resumes_same_attempt(self):
        from human_compact.trajectory import project_package_manager as PM
        plan=self.plan();id=self.retained()
        request={'manager':'bun','version':'1.3.14','directory':'/managed/bun'}
        with mock.patch.object(S,'propose',return_value=plan),mock.patch.object(PM,'check_runtime',side_effect=[PM.MissingBun(),None,None]),mock.patch.object(PM,'bun_request',return_value=request),mock.patch.object(PM,'install_bun') as install:
            R.start(id)
            deadline=time.monotonic()+5
            while R.view(id)['status']!='awaiting_approval' and time.monotonic()<deadline:time.sleep(.02)
            state=R.view(id);self.assertEqual('awaiting_approval',state['status'])
            install.assert_not_called()
            approval=state['approval'];self.assertEqual('bun',approval['kind'])
            R.decide_approval(id,approval['id'],True)
            state=self.wait(id)
            self.assertEqual('running',state['status'],state)
            install.assert_called_once()
            self.assertEqual(request,install.call_args.args[0])
            self.assertEqual(1,len(state['attempts']))

    def test_execution_failure_survives_rejected_repair(self):
        failed=self.plan(fail=True)
        rejected=self.plan(fail=True);rejected['services'][0]['argv']=['npm','install','--force']
        id=self.retained()
        with mock.patch.object(S,'propose',side_effect=[failed,rejected,{'status':'needs_input','reason':'Review runtime'}]) as propose:
            R.start(id);result=self.wait(id)
        self.assertEqual('needs_input',result['status'])
        feedback=propose.call_args.args[1]
        self.assertEqual('validation',feedback['stage'])
        self.assertEqual(9,feedback['originalFailure']['exitCode'])
        self.assertEqual('backend',feedback['originalFailure']['stage'])

    def test_unsupported_rechecks_documented_bun_workflow(self):
        sub=self.root/'app';sub.mkdir()
        (sub/'package.json').write_text(json.dumps({'packageManager':'bun@1.3.14','scripts':{'dev':'next dev'}}))
        (sub/'bun.lock').write_text('{}')
        (sub/'README.md').write_text('Install with bun install; launch with bun run dev.')
        engine=mock.Mock()
        engine.generate_plain.side_effect=[json.dumps({'status':'unsupported','reason':'npm peer conflict requires prohibited flags'}),
            json.dumps({'status':'needs_input','reason':'The repository requires Bun; install the declared Bun runtime.'})]
        failure={'stage':'validation','originalFailure':{'stage':'install','command':'npm install','stderr':'ERESOLVE react peer conflict','componentCwd':'app'},'componentCwd':'app'}
        with mock.patch.object(PV,'_engine',return_value=engine):
            result=S.propose({'cwd':str(self.root)},failure,[],str)
        self.assertEqual('needs_input',result['status'])
        self.assertEqual(2,engine.generate_plain.call_count)
        brief=json.loads(engine.generate_plain.call_args.args[0].split('Evidence JSON:\n',1)[1])
        self.assertEqual('bun',brief['packageManager']['manager'])
        self.assertIn('bun install',str(brief['files']))
        self.assertIn('terminalReview',brief)
        self.assertEqual('npm install',brief['failure']['originalFailure']['command'])

    def test_terminal_review_is_bounded(self):
        engine=mock.Mock();engine.generate_plain.return_value=json.dumps({'status':'unsupported','reason':'Documented runtime unsupported'})
        with mock.patch.object(PV,'_engine',return_value=engine):
            result=S.propose({'cwd':str(self.root)},{'stage':'install'},[],str)
        self.assertEqual('unsupported',result['status'])
        self.assertEqual(2,engine.generate_plain.call_count)

    def test_repair_receives_failed_component_and_verified_runtime_history(self):
        backend=self.root/'system'/'backend';backend.mkdir(parents=True)
        (backend/'requirements.txt').write_text('aiohttp==3.8.4\nfrozenlist==1.3.3\n')
        (backend.parent/'README.md').write_text('Backend setup instructions')
        (self.root/'README.md').write_text('Research paper overview')
        engine=mock.Mock();engine.generate_plain.return_value=json.dumps({'status':'needs_input','reason':'Choose compatible Python'})
        runtime={'actualVersion':'3.12.13','requestedVersion':'3.12','cwd':'system/backend'}
        failure={'stage':'backend-deps','componentCwd':'system/backend','runtime':runtime,'stderr':'ob_digit missing'}
        history=[{'status':'failed','executedRuntimes':[{'actualVersion':'3.14.2'}]},
                 {'status':'failed','executedRuntimes':[runtime],'failure':failure}]
        with mock.patch.object(PV,'_engine',return_value=engine):
            S.propose({'cwd':str(self.root)},failure,history,lambda x:x)
        brief=json.loads(engine.generate_plain.call_args.args[0].split('Evidence JSON:\n',1)[1])
        self.assertEqual('system/backend/requirements.txt',brief['files'][0]['path'])
        self.assertEqual('3.12.13',brief['failure']['runtime']['actualVersion'])
        self.assertEqual('3.14.2',brief['previousAttempts'][0]['executedRuntimes'][0]['actualVersion'])
        self.assertIn('Backend setup instructions',str(brief['files']))

    def test_port_conflict_returns_to_repair_without_claiming_listener(self):
        good=self.plan()
        good['services']=good['services'][:1];good['entryService']='backend'
        bad=json.loads(json.dumps(good))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1',0));listener.listen()
            bad['services'][0]['healthUrl']='http://127.0.0.1:'+str(listener.getsockname()[1])
            id=self.retained()
            with mock.patch.object(S,'propose',side_effect=[bad,good]) as propose:
                R.start(id);result=self.wait(id)
                self.assertEqual('running',result['status'],result)
                feedback=propose.call_args_list[1].args[1]
                self.assertEqual('ports',feedback['stage'])
                self.assertIn('originalFailure',feedback)
                self.assertGreater(feedback['suggestedPort'],1023)
                self.assertEqual('',feedback['stdout'])
                self.assertIsNone(feedback['exitCode'])
                self.assertEqual(2,propose.call_count)
                self.assertNotEqual(result['url'],bad['services'][0]['healthUrl'])

    def test_reset_during_planning_cannot_launch(self):
        import threading
        started=threading.Event();release=threading.Event();plan=self.plan()
        def proposal(*args):started.set();release.wait(5);return plan
        id=self.retained()
        with mock.patch.object(S,'propose',side_effect=proposal),mock.patch.object(PV,'start_plan_process') as launch:
            R.start(id);self.assertTrue(started.wait(2));R.reset(id);release.set();time.sleep(.3)
            launch.assert_not_called();self.assertEqual('failed',R.view(id)['status'])

if __name__=='__main__':unittest.main()

class NextLaunchOptionsTests(unittest.TestCase):
    def test_next_loopback_override_and_reject_unsafe_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory).resolve()
            def validate(script,args):
                (root/'package.json').write_text(json.dumps({'scripts':{'start':script}}))
                return S.command(root,{'cwd':'.','argv':['npm','run','start','--']+args},False)
            result=validate('next start -p 3200',['--port','3201','--hostname','127.0.0.1'])
            self.assertEqual(result['argv'][-1],'127.0.0.1')
            for script,args in [
                ('next start -p 3200',['--hostname','0.0.0.0']),
                ('next start -p 3200',['--port','80']),
                ('next start -p 3200',['--strictPort']),
                ('next build',['--port','3201']),
                ('next start;echo unsafe',['--port','3201']),
                ('node wrapper.js',['--port','3201'])]:
                with self.subTest(script=script,args=args),self.assertRaises(ValueError):validate(script,args)
