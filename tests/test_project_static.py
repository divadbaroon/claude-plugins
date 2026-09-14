import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from unittest import mock
from human_compact.trajectory import project_static as PS, project_analysis as PA, project_components as C, project_setup as S, project_run as R, preview as PV
from test_project_setup import SetupTests

class StaticTests(SetupTests):
    def site(self):
        site=self.root/'research'/'visualization';site.mkdir(parents=True)
        (site/'index.html').write_text('<html><script src="data.js"></script><h1>Static fixture</h1></html>')
        (site/'data.js').write_text('window.data=[1,2,3];')
        return site
    def test_nested_static_entrypoint_is_discovered_without_package_manifest(self):
        (self.root/'package.json').unlink();site=self.site()
        child=site/'pages';child.mkdir();(child/'index.html').write_text('A child page')
        discovery=C.discover(str(self.root))
        self.assertEqual(['research/visualization'],[c['id'] for c in discovery['components']])
        self.assertEqual(['staticfile'],discovery['components'][0]['types'])
    def test_build_config_is_not_misclassified_as_plain_static(self):
        site=self.site();(site/'package.json').write_text('{}')
        self.assertFalse(PS.eligible(site))
        self.assertEqual(['node'],next(c for c in C.discover(str(self.root))['components'] if c['path']==str(site.resolve()))['types'])
    def test_static_analysis_needs_no_railpack_or_install(self):
        site=self.site();before={p.name:p.read_bytes() for p in site.iterdir()}
        with mock.patch.object(PA.subprocess,'Popen') as launch,mock.patch.object(PA.shutil,'which',return_value=None):
            result=PA.analyze(str(site),str(self.root))
        self.assertTrue(result['ok']);self.assertEqual('native-static',result['source']);launch.assert_not_called()
        self.assertEqual([],result['nativePlan']['preparation'])
        self.assertEqual(before,{p.name:p.read_bytes() for p in site.iterdir()})
    def test_real_static_launch_assets_health_and_reset(self):
        site=self.site();(site/'.env').write_text('PRIVATE_VALUE=do-not-serve')
        (site/'empty').mkdir();(site/'escape.txt').symlink_to(self.root/'README.md')
        before={p.name for p in site.iterdir()}
        with mock.patch.object(S,'propose',side_effect=AssertionError('Static launch must not require a model')):
            result=PA.analyze(str(site),str(self.root));id=result['analysisId'];R.start(id);state=self.wait(id)
            self.assertEqual('running',state['status'],state)
            with urllib.request.urlopen(state['url'],timeout=3) as response:
                self.assertEqual(200,response.status);self.assertIn(b'Static fixture',response.read())
            with urllib.request.urlopen(state['url']+'data.js',timeout=3) as response:self.assertIn(b'window.data',response.read())
            for path in ('.env','%2eenv','escape.txt','empty/','%2e%2e/README.md'):
                with self.subTest(path=path),self.assertRaises(urllib.error.HTTPError) as error:
                    urllib.request.urlopen(state['url']+path,timeout=3)
                self.assertEqual(404,error.exception.code)
            self.assertEqual(before,{p.name for p in site.iterdir()})
            self.assertEqual(1,len(state['stages']))
            R.reset(id);self.assertFalse(R._JOBS[id]['proc'].alive())
    def test_repair_brief_exposes_static_capability_without_readme(self):
        site=self.site();engine=mock.Mock()
        engine.generate_plain.return_value=json.dumps({'status':'needs_input','reason':'Example'})
        with mock.patch.object(PV,'_engine',return_value=engine):
            S.propose({'cwd':str(site),'repositoryRoot':str(self.root)},
                      {'stage':'build','stderr':'caddy: command not found'},[],lambda text:text)
        brief=json.loads(engine.generate_plain.call_args.args[0].split('Evidence JSON:\n',1)[1])
        self.assertEqual('static',brief['availableServices'][0]['kind'])
        self.assertEqual('research/visualization',brief['availableServices'][0]['cwd'])
        self.assertEqual(['hc-static'],brief['availableServices'][0]['argv'])

    def test_second_analysis_joins_owned_run_without_starting_another_process(self):
        site=self.site();first=PA.analyze(str(site),str(self.root))['analysisId']
        R.start(first);running=self.wait(first);self.assertEqual('running',running['status'])
        second=PA.analyze(str(site),str(self.root))['analysisId']
        with mock.patch.object(PV,'start_plan_process') as launch:
            joined=R.start(second)
            launch.assert_not_called()
        self.assertTrue(joined['joinedExistingRun'])
        self.assertEqual(first,joined['id']);self.assertEqual(running['pid'],joined['pid'])
        self.assertNotIn(second,R._JOBS)
        self.assertEqual(running['url'],joined['url'])
        R.reset(joined['id']);self.assertFalse(R._JOBS[first]['proc'].alive())

    def other_server(self):
        from http.server import SimpleHTTPRequestHandler,ThreadingHTTPServer
        from functools import partial
        import threading
        directory=Path(self.temp.name)/'unrelated';directory.mkdir()
        (directory/'index.html').write_text('Unrelated application')
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),partial(Quiet,directory=str(directory)))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        return 'http://127.0.0.1:'+str(server.server_address[1])+'/'

    def test_occupied_static_port_moves_and_leaves_other_application_running(self):
        site=self.site();original=self.other_server()
        id=PA.analyze(str(site),str(self.root))['analysisId'];record=R.read(id)
        record['orderPlan']['services'][0]['healthUrl']=original;R.write(id,record)
        with mock.patch.object(S,'propose',side_effect=AssertionError('No repair needed')):
            R.start(id);state=self.wait(id)
        self.assertEqual('running',state['status'],state)
        self.assertNotEqual(original,state['url'])
        self.assertEqual(original,state['portChanges'][0]['requestedUrl'])
        self.assertEqual(state['url'],state['portChanges'][0]['actualUrl'])
        with urllib.request.urlopen(state['url']) as response:self.assertIn(b'Static fixture',response.read())
        R.reset(id)
        with urllib.request.urlopen(original) as response:self.assertIn(b'Unrelated application',response.read())
        self.assertFalse(list(R.folder().glob('.static-ready-*')))

    def test_fixed_service_conflict_stops_before_preparation(self):
        original=self.other_server();plan=self.plan()
        plan['services'][0]['healthUrl']=original
        plan['preparation']=[{'id':'deps','cwd':'.','argv':['npm','install']}]
        id=self.retained();record=R.read(id);record['orderPlan']=plan;R.write(id,record)
        with mock.patch.object(PV,'start_plan_process') as launch,mock.patch.object(S,'propose') as propose:
            R.start(id);state=self.wait(id)
            self.assertEqual('needs_input',state['status'],state)
            self.assertEqual('ports',state['stage']);launch.assert_not_called();propose.assert_not_called()
        self.assertEqual(['frontend'],state['portConflicts'][0]['dependents'])
        self.assertEqual(original,R.read(id)['orderPlan']['services'][0]['healthUrl'])
        with urllib.request.urlopen(original) as response:self.assertIn(b'Unrelated application',response.read())

    def test_linked_static_port_is_not_silently_changed(self):
        original=self.other_server();site=self.site();plan=PS.plan(site,self.root)
        plan['services'][0]['healthUrl']=original
        other=self.plan()['services'][0];other['dependsOn']=['static'];plan['services'].append(other)
        id=self.retained();record=R.read(id);record['orderPlan']=plan;R.write(id,record)
        with mock.patch.object(PV,'start_plan_process') as launch:
            R.start(id);state=self.wait(id)
            self.assertEqual('needs_input',state['status'],state);launch.assert_not_called()
        self.assertEqual(original,state['portConflicts'][0]['healthUrl'])

    def test_invalid_static_capability_rejected(self):
        site=self.site();plan=PS.plan(site,self.root)
        for change in ({'argv':['python3','-c','print(1)']},{'healthUrl':'http://localhost:8765/'},{'cwd':'.'}):
            bad=json.loads(json.dumps(plan));bad['services'][0].update(change)
            with self.assertRaises(ValueError):S.validate(self.root,bad)
        (site/'Caddyfile').write_text('custom config')
        with self.assertRaises(ValueError):S.validate(self.root,plan)
