import json
import os
from pathlib import Path
import shlex
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from human_compact.trajectory import project_order as O, project_run as R, project_setup as S, preview as PV

class OrderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()/'repo';self.root.mkdir()
        self.patch=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(self.root.parent/'home')});self.patch.start();self.addCleanup(self.patch.stop)
        self.backend=self.root/'system/backend';self.frontend=self.root/'system/frontend'
        self.backend.mkdir(parents=True);self.frontend.mkdir(parents=True)
        (self.backend/'requirements.txt').write_text('flask\n')
        (self.root/'system/README.md').write_text('Run both commands from frontend. npm run backend serves 8090, then npm start serves 8080.\n')
        (self.frontend/'package.json').write_text(json.dumps({'scripts':{'backend':'cd ../backend && .venv/bin/python base.py','start':'node frontend.js'},'proxy':'http://localhost:8090'}))
        self.addCleanup(self.cleanup)
    def cleanup(self):
        for id in list(R._JOBS):R.reset(id)
        time.sleep(.6);R._JOBS.clear()
    def plan(self):
        return {'status':'plan','summary':'Run both services','orderingRationale':'Backend-first is a conservative choice, not a strict startup requirement',
            'selectedComponents':['system/backend','system/frontend'],'excludedComponents':[],
            'evidence':['system/README.md:1','system/frontend/package.json:1'],
            'preparation':[], 'services':[
            {'id':'backend','cwd':'system/frontend','argv':['npm','run','backend'],'dependsOn':[],'healthUrl':'http://127.0.0.1:8090'},
            {'id':'frontend','cwd':'system/frontend','argv':['npm','start'],'dependsOn':['backend'],'healthUrl':'http://127.0.0.1:8080'}], 'entryService':'frontend'}
    def test_initial_invalid_citation_gets_bounded_correction(self):
        discovery=O.PC.discover(str(self.root))
        invalid=self.plan();invalid['evidence']=['README table without line numbers']
        engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps(invalid),json.dumps(self.plan())]
        with mock.patch.object(PV,'_engine',return_value=engine):result=O.assess(self.root,discovery,[],str)
        O.apply_assessment({'order':{}},self.root,discovery['components'],result)
        self.assertEqual(2,engine.generate_plain.call_count)
        brief=json.loads(engine.generate_plain.call_args.args[0].split('Evidence JSON:\n')[1])
        self.assertIn('Evidence must cite',brief['validationError'])
        self.assertEqual(invalid,brief['initialAssessment'])

    def test_followup_receives_citation_and_missing_root_together(self):
        discovery=O.PC.discover(str(self.root))
        discovery['components'].append({'id':'.','path':str(self.root),'evidence':[]})
        invalid=self.plan();invalid['evidence']=['system/README.md (setup instructions)']
        corrected=self.plan();corrected['excludedComponents']=[{'id':'.','reason':'Workspace dispatcher, not a separate app'}]
        engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps(invalid),json.dumps(corrected)]
        with mock.patch.object(PV,'_engine',return_value=engine):result=O.assess(self.root,discovery,[],str)
        brief=json.loads(engine.generate_plain.call_args.args[0].split('Evidence JSON:\n')[1])
        self.assertTrue(any('evidence[0]' in x for x in brief['validationErrors']))
        self.assertTrue(any("missing IDs: '.'" in x for x in brief['validationErrors']))
        record={'order':{}};O.apply_assessment(record,self.root,discovery['components'],result)
        self.assertEqual('done',record['order']['status'])
        self.assertEqual(2,engine.generate_plain.call_count)

    def test_unlocated_notes_do_not_override_validated_plan(self):
        plan=self.plan();plan['evidence'].append('system/README.md (local setup)')
        record={'order':{}};O.apply_assessment(record,self.root,O.PC.discover(str(self.root))['components'],plan)
        self.assertEqual('done',record['order']['status'])
        self.assertEqual(['system/README.md (local setup)'],record['order']['evidenceNotes'])
        self.assertEqual(self.plan()['evidence'],record['orderPlan']['evidence'])
        plan['evidence']=['system/README.md (local setup)']
        with self.assertRaisesRegex(ValueError,'Evidence must cite'):O.apply_assessment({'order':{}},self.root,O.PC.discover(str(self.root))['components'],plan)

    def railpack(self,path,root):
        return {'ok':True,'analysisId':'fake','info':{'detectedProviders':['python' if path.endswith('backend') else 'node']},'plan':{'steps':[{'name':'install','commands':[{'cmd':'npm install'}]}],'deploy':{'startCommand':'npm start'}}}
    def wait(self,id):
        deadline=time.monotonic()+8
        while time.monotonic()<deadline:
            result=O.view(id)
            if result['status'] not in ('analyzing','assessing'):return result
            time.sleep(.03)
        self.fail(str(result))
    def test_assesses_all_components_before_execution_and_persists(self):
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack) as analyze,mock.patch.object(O,'assess',return_value=self.plan()) as assess,mock.patch.object(PV,'start_plan_process') as launch:
            result=self.wait(O.start(str(self.root))['id'])
            self.assertEqual('done',result['status'],result)
            self.assertEqual(2,analyze.call_count);self.assertEqual(1,assess.call_count)
            launch.assert_not_called()
            self.assertEqual(['system/frontend','system/frontend'],[s['cwd'] for s in result['plan']['services']])
            self.assertEqual('ready',R.view(result['id'])['status'])
            self.assertEqual('run_order',R.view(result['id'])['source'])
            self.assertEqual(result['plan'],O.view(result['id'])['plan'])
            self.assertEqual(self.plan()['orderingRationale'],result['orderingRationale'])
    def test_duplicate_request_joins_and_interrupted_reload_is_explicit(self):
        entered=threading.Event();release=threading.Event()
        def assess(*args):entered.set();release.wait(3);return self.plan()
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(O,'assess',side_effect=assess) as call:
            first=O.start(str(self.root));self.assertTrue(entered.wait(2))
            self.assertEqual(first['id'],O.start(str(self.root))['id'])
            self.assertEqual(1,call.call_count);release.set();self.wait(first['id'])
        record=R.read(first['id']);record['order']['status']='assessing';R.write(first['id'],record)
        self.assertEqual('error',O.view(first['id'])['status'])
    def test_ambiguous_and_invalid_plans_do_not_run(self):
        bad=self.plan();bad['services'][0]['cwd']='../outside'
        for answer in ({'status':'needs_input','reason':'Which of these unrelated apps do you want?'},bad):
            with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(O,'assess',return_value=answer),mock.patch.object(PV,'start_plan_process') as launch:
                result=self.wait(O.start(str(self.root))['id'])
                self.assertIn(result['status'],('needs_input','error'));launch.assert_not_called()
                with self.assertRaises(ValueError):R.start(result['id'])
    def test_bounded_agent_context_and_targeted_read(self):
        (self.root/'.env.local').write_text('TOKEN=hidden-value\n')
        (self.backend/'base.py').write_text("@app.route('/')\ndef index(): return 'healthy'\n")
        engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps({'status':'read_more','files':['system/backend/base.py']}),json.dumps(self.plan())]
        discovery=O.PC.discover(str(self.root));redact=O._redactor(self.root,discovery['components'])
        (self.root/'system/README.md').write_text('TOKEN=hidden-value\nRun both services from frontend.\n')
        with mock.patch.object(PV,'_engine',return_value=engine):
            result=O.assess(self.root,discovery,[{'component':'system/frontend','startCommand':'npm start'}],redact)
        self.assertEqual('plan',result['status'])
        self.assertEqual(2,engine.generate_plain.call_count)
        for call in engine.generate_plain.call_args_list:
            self.assertNotIn('hidden-value',call.args[0]);self.assertLessEqual(len(call.args[0]),36000)
        prompt=engine.generate_plain.call_args.args[0]
        self.assertIn('npm run',prompt);self.assertIn('railpack',prompt);self.assertIn('base.py',prompt)
        engine.generate_searching.assert_not_called()
        with self.assertRaises(ValueError):O._excerpt(self.root,'.env.local',redact)
    @unittest.skipUnless(shutil.which('npm'),'npm required for shared-cwd integration fixture')
    def test_assessed_plan_runs_both_commands_from_frontend_without_repair(self):
        def port():
            with socket.socket() as s:s.bind(('127.0.0.1',0));return s.getsockname()[1]
        backend_port,frontend_port=port(),port()
        server="import http.server\ns=http.server.HTTPServer(('127.0.0.1',PORT),http.server.SimpleHTTPRequestHandler)\ns.serve_forever()\n"
        (self.backend/'.env.local').write_text('PRIVATE_TOKEN=private-fixture-token\n')
        (self.backend/'base.py').write_text("import os; assert os.environ['PRIVATE_TOKEN']=='private-fixture-token'\n"+server.replace('PORT',str(backend_port)))
        (self.frontend/'frontend.py').write_text(server.replace('PORT',str(frontend_port)))
        (self.backend/'.venv/bin').mkdir(parents=True);(self.backend/'.venv/bin/python').symlink_to(sys.executable)
        (self.frontend/'package.json').write_text(json.dumps({'scripts':{'backend':'cd ../backend && .venv/bin/python base.py','start':shlex.quote(sys.executable)+' frontend.py'}}))
        plan=self.plan();plan['services'][0]['environmentCwd']='system/backend';plan['services'][0]['healthUrl']='http://127.0.0.1:'+str(backend_port);plan['services'][1]['healthUrl']='http://127.0.0.1:'+str(frontend_port)
        plan['services'][0]['id']='system/backend'
        plan['services'][1].update(id='system/frontend',dependsOn=['system/backend'])
        plan['entryService']='system/frontend'
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(O,'assess',return_value=plan):
            assessed=self.wait(O.start(str(self.root))['id'])
        self.assertEqual('done',assessed['status'],assessed)
        with mock.patch.object(S,'propose',side_effect=AssertionError('Repair must not run on success')):
            id=assessed['id'];R.start(id)
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                run=R.view(id)
                if run['status'] in ('running','failed','needs_input'):break
                time.sleep(.05)
            self.assertEqual('running',run['status'],run)
            self.assertEqual(plan['services'][1]['healthUrl'],run['url'])
            self.assertEqual(2,len(R._JOBS[id]['setupProcs']))
            self.assertEqual([str(self.frontend)]*2,[s['cwd'] for s in run['stages']])
            self.assertEqual([],run['failures'])
            self.assertNotIn('private-fixture-token',json.dumps(run))
            R.reset(id)
            self.assertFalse(any(p.alive() for p in R._JOBS[id]['setupProcs'].values()))
    def test_container_fixture_reaches_agent_and_can_be_excluded(self):
        fixture=self.root/'tooling/test';fixture.mkdir(parents=True)
        (fixture/'compose.yml').write_text('services:\n  fixture:\n    image: fixture\n')
        (fixture/'README.md').write_text('Only a compatibility test fixture; not part of the application.')
        proposal=self.plan();proposal['excludedComponents']=[{'id':'tooling/test/compose.yml#fixture','reason':'README identifies an unrelated compatibility test'}]
        engine=mock.Mock();engine.generate_plain.return_value=json.dumps(proposal)
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack) as analyze,mock.patch.object(PV,'_engine',return_value=engine),mock.patch.object(PV,'start_plan_process') as launch:
            result=self.wait(O.start(str(self.root))['id'])
            self.assertEqual('done',result['status'],result)
            self.assertEqual(1,engine.generate_plain.call_count)
            self.assertEqual(2,analyze.call_count);launch.assert_not_called()
            prompt=engine.generate_plain.call_args.args[0]
            self.assertIn('tooling/test/compose.yml#fixture',prompt)
            self.assertIn('Only a compatibility test fixture',prompt)

    def test_selected_container_blocks_only_after_agent_selection(self):
        (self.root/'compose.yml').write_text('services:\n  db:\n    image: postgres\n')
        proposal=self.plan();proposal['selectedComponents'].append('compose.yml#db')
        proposal.update(preparation=[],services=[],entryService='')
        proposal['orderingRationale']='The application requires the Compose database.'
        engine=mock.Mock();engine.generate_plain.return_value=json.dumps(proposal)
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(PV,'_engine',return_value=engine),mock.patch.object(PV,'start_plan_process') as launch:
            result=self.wait(O.start(str(self.root))['id'])
            self.assertEqual('needs_input',result['status'],result)
            self.assertIn('compose.yml#db',result['reason'])
            self.assertEqual(1,engine.generate_plain.call_count);launch.assert_not_called()
            self.assertNotIn('orderPlan',R.read(result['id']))

    def test_large_inventory_reaches_assessment_with_bounded_railpack(self):
        for i in range(12):
            folder=self.root/('library'+str(i));folder.mkdir()
            (folder/'package.json').write_text('{}')
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack) as analyze,mock.patch.object(O,'assess',return_value={'status':'needs_input','reason':'Select CLI or web interface'}) as assess:
            result=self.wait(O.start(str(self.root))['id'])
            self.assertEqual('needs_input',result['status'])
            self.assertEqual(14,len(assess.call_args.args[1]['components']))
            self.assertEqual(O.MAX_RAILPACK_COMPONENTS,analyze.call_count)

    def test_live_prompt_and_timeout_are_persisted(self):
        entered=threading.Event();release=threading.Event()
        engine=mock.Mock(model='test-model',kind='claude')
        def infer(prompt,**kwargs):
            self.assertTrue(kwargs['planning']);entered.set();release.wait(3)
            raise RuntimeError('claude CLI timed out after 90s')
        engine.generate_plain.side_effect=infer
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(PV,'_engine',return_value=engine):
            first=O.start(str(self.root));self.assertTrue(entered.wait(2))
            live=O.view(first['id']);call=live['agentTrace']['calls'][0]
            self.assertEqual('running',call['status'])
            self.assertEqual(engine.generate_plain.call_args.args[0],call['prompt'])
            self.assertEqual('test-model',call['model']);self.assertEqual(90,call['timeoutSeconds'])
            release.set();done=self.wait(first['id'])
            failed=done['agentTrace']['calls'][0]
            self.assertEqual('timed_out',failed['status']);self.assertIn('90s',failed['error'])
            self.assertIn('durationSeconds',failed)
            self.assertEqual(failed,O.view(first['id'])['agentTrace']['calls'][0])

    def test_line_ranges_are_accepted_but_post_health_is_rejected(self):
        plan=self.plan();plan['evidence']=['system/README.md:20-34']
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(O,'assess',return_value=plan):
            result=self.wait(O.start(str(self.root))['id'])
            self.assertEqual('done',result['status'],result)
        (self.backend/'base.py').write_text('@app.route("/", methods=["GET"])\ndef index(): return "OK"\n@app.route("/get-num-problem", methods=["POST"])\ndef num(): return {}\n')
        plan['services'][0]['environmentCwd']='system/backend'
        plan['services'][0]['healthUrl']='http://127.0.0.1:8090/get-num-problem'
        with self.assertRaisesRegex(ValueError,'does not accept GET'):S.validate(self.root,plan)
        plan['services'][0]['healthUrl']='http://127.0.0.1:8090/'
        self.assertEqual('plan',S.validate(self.root,plan)['status'])

    def test_annotated_citations_and_saved_revalidation(self):
        valid=['system/README.md:20 (backend install: venv+pip)',
               'system/README.md:9-20 (installation)',
               'system/frontend/package.json:16 ("backend": "cd ../backend && .venv/bin/python base.py")']
        for citation in valid:self.assertTrue(O.validate_citation(self.root,citation))
        for citation in ('system/README.md:0','system/README.md:20-9','../secret:1','system/README.md:nan','missing.py:1'):
            with self.assertRaises((ValueError,OSError)):O.validate_citation(self.root,citation)
        plan=self.plan();plan['evidence']=valid
        plan['services'][0]['id']='system/backend'
        plan['services'][1].update(id='system/frontend',dependsOn=['system/backend'])
        plan['entryService']='system/frontend'
        with mock.patch.object(O.PA,'analyze',side_effect=self.railpack),mock.patch.object(O,'assess',return_value=plan):
            result=self.wait(O.start(str(self.root))['id'])
        self.assertEqual('done',result['status'],result)
        record=R.read(result['id']);record.pop('orderPlan');record['order'].update(status='error',error='Invalid or duplicate setup step ID',agentTrace={'calls':[{'status':'done','response':json.dumps(plan)}]})
        R.write(result['id'],record)
        with mock.patch.object(O,'assess',side_effect=AssertionError('No inference')),mock.patch.object(PV,'start_plan_process',side_effect=AssertionError('No execution')):
            restored=O.revalidate_saved(result['id'])
        self.assertEqual('done',restored['status']);self.assertNotIn('error',restored)
        self.assertEqual(plan['services'][0]['argv'],restored['plan']['services'][0]['argv'])

    def test_missing_metadata_recovered_only_with_unambiguous_service_coverage(self):
        plan=self.plan();plan.pop('selectedComponents');plan.pop('excludedComponents');plan.pop('orderingRationale')
        plan['services'][0]['environmentCwd']='system/backend'
        record={'order':{}}
        O.apply_assessment(record,self.root,O.PC.discover(str(self.root))['components'],plan)
        self.assertEqual(['system/backend','system/frontend'],record['order']['selectedComponents'])
        self.assertIn('omitted',record['order']['orderingRationale'])
        self.assertEqual(plan['services'][0]['argv'],record['orderPlan']['services'][0]['argv'])
        (self.root/'unrelated').mkdir();(self.root/'unrelated/requirements.txt').write_text('flask')
        with self.assertRaisesRegex(ValueError,'supported component IDs'):
            O.apply_assessment({'order':{}},self.root,O.PC.discover(str(self.root))['components'],plan)
        example=O.POLICY[O.POLICY.index('{"status":"plan"'):O.POLICY.index('{"status":"needs_input"')]
        for field in ('selectedComponents','excludedComponents','orderingRationale'):self.assertIn(field,example)

    def test_route_uses_local_boundary(self):
        from test_goal_page import server_for,post_json
        chat=self.root/'chat';chat.mkdir()
        with server_for(chat) as url,mock.patch.object(O,'start',return_value={'id':'order','status':'analyzing'}) as start:
            answer=post_json(url+'/api/op',{'op':'project_order_start','path':str(self.root)})
            self.assertTrue(answer['ok']);start.assert_called_once_with(str(self.root))

if __name__=='__main__':unittest.main()

class EvidenceFollowupTests(unittest.TestCase):
    def test_fresh_assessment_includes_readme_and_reconsiders_needs_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve()
            (root/'README.md').write_text('Use the public viewer by default. Admin is separate.')
            for name in ('apps/viewer','apps/admin','packages/shared','packages/renderer'):
                (root/name).mkdir(parents=True)
                (root/name/'package.json').write_text('{"scripts":{}}')
            (root/'package.json').write_text(json.dumps({'scripts':{'dev':'npm run dev:viewer','dev:viewer':'vite --config apps/viewer/vite.config.ts'}}))
            (root/'apps/viewer/vite.config.ts').write_text('export default {}')
            discovery=O.PC.discover(str(root))
            engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps({'status':'needs_input','reason':'Viewer or admin?'}),json.dumps({'status':'plan','services':[{'id':'viewer'}]})]
            with mock.patch.object(PV,'_engine',return_value=engine):result=O.assess(root,discovery,[],str)
            self.assertEqual('plan',result['status']);self.assertEqual(2,engine.generate_plain.call_count)
            briefs=[json.loads(c.args[0].split('Evidence JSON:\n')[1]) for c in engine.generate_plain.call_args_list]
            self.assertIn('README.md',[f['path'] for f in briefs[0]['files']])
            self.assertEqual('apps/viewer',briefs[0]['declaredDefault']['targetComponent'])
            self.assertEqual('needs_input',briefs[1]['initialAssessment']['status'])
            self.assertIn('apps/viewer/vite.config.ts',[f['path'] for f in briefs[1]['requestedFiles']])
    def test_true_ambiguity_still_pauses_after_one_followup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'README.md').write_text('Two independent applications; no default.')
            discovery={'components':[],'relationships':[],'dependencies':[],'documentation':['README.md']}
            engine=mock.Mock();engine.generate_plain.return_value=json.dumps({'status':'needs_input','reason':'Which application?'})
            with mock.patch.object(PV,'_engine',return_value=engine):result=O.assess(root,discovery,[],str)
            self.assertEqual('needs_input',result['status']);self.assertEqual(2,engine.generate_plain.call_count)
    def test_plan_with_unrelated_services_receives_default_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp).resolve();(root/'apps/viewer').mkdir(parents=True)
            (root/'apps/viewer/vite.config.ts').write_text('export default {}')
            (root/'package.json').write_text(json.dumps({'scripts':{'dev':'vite --config apps/viewer/vite.config.ts'}}))
            discovery=O.PC.discover(str(root))
            both={'status':'plan','services':[{'id':'viewer','dependsOn':[]},{'id':'admin','dependsOn':[]}]}
            engine=mock.Mock();engine.generate_plain.side_effect=[json.dumps(both),json.dumps({'status':'plan','services':[{'id':'viewer'}]})]
            with mock.patch.object(PV,'_engine',return_value=engine):result=O.assess(root,discovery,[],str)
            self.assertEqual(1,len(result['services']));self.assertEqual(2,engine.generate_plain.call_count)
            self.assertIn('Check extra independent services',engine.generate_plain.call_args.args[0])
