import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock
from human_compact.trajectory import project_supabase as S, project_environment as PE, project_run as R

class LocalSupabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()/'repo';self.root.mkdir()
        self.patch=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(Path(self.tmp.name)/'home')});self.patch.start();self.addCleanup(self.patch.stop)
        self.write('package.json','{}')
        self.write('app.js','process.env.NEXT_PUBLIC_SUPABASE_URL; process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY; process.env.SUPABASE_SERVICE_ROLE_KEY; process.env.STRIPE_SECRET_KEY;')
        self.write('supabase/config.toml','project_id="production-name"\n[api]\nport=54321\n[db]\nport=54322\nshadow_port=54320\n[db.seed]\nenabled=true\nsql_paths=["./seed.sql"]\n[auth.external.google]\nenabled=true\nsecret="env(REMOTE_SECRET)"\n')
        self.write('supabase/migrations/001_schema.sql','create table public.example(id int);')
        self.target=S.location(str(self.root))[1]
    def write(self,name,text):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    def wait(self):
        for _ in range(200):
            if str(self.target) not in S._JOBS:return S.inspect(str(self.root))
            time.sleep(.01)
        self.fail('worker did not finish')
    def test_isolated_inputs_and_missing_seed(self):
        self.write('supabase/.temp/project-ref','production-ref')
        self.write('supabase/.env','SERVICE_ROLE_KEY=production-secret')
        before=(self.root/'supabase/config.toml').read_bytes()
        work,ports,digest,warnings=S.prepare(self.root,self.target,self.root)
        config=S.tomllib.loads((work/'supabase/config.toml').read_text())
        self.assertNotEqual(config['project_id'],'production-name')
        self.assertEqual(len(ports),len(set(ports.values())))
        self.assertFalse((work/'supabase/.temp').exists());self.assertFalse((work/'supabase/.env').exists())
        self.assertFalse(config['db']['seed']['enabled']);self.assertNotIn('external',config['auth'])
        self.assertTrue(any('Missing seed' in w for w in warnings));self.assertEqual(before,(self.root/'supabase/config.toml').read_bytes())
    def test_no_detection_from_sdk_alone(self):
        (self.root/'supabase/config.toml').unlink()
        self.assertFalse(S.detect(str(self.root))['available'])
    def test_nested_component_finds_ancestor(self):
        child=self.root/'frontend';child.mkdir()
        self.assertEqual(S.detect(str(child),str(self.root))['source'],str(self.root))
    def test_network_sql_rejected_before_execution(self):
        self.write('supabase/seed.sql',"select net.http_post('https://production.example');")
        with self.assertRaisesRegex(ValueError,'network operations'):S.prepare(self.root,self.target,self.root)
    def test_symlink_inputs_rejected(self):
        (self.root/'supabase/migrations/evil.sql').symlink_to(self.root/'package.json')
        with self.assertRaisesRegex(ValueError,'symlinks'):S.prepare(self.root,self.target,self.root)
    def test_schema_change_preserves_existing_workspace(self):
        work,ports,digest,warnings=S.prepare(self.root,self.target,self.root)
        S.write_state(self.target,{'ports':ports,'schemaDigest':digest})
        self.write('supabase/migrations/002_schema.sql','drop table public.example;')
        with self.assertRaisesRegex(ValueError,'schema/config changed'):S.prepare(self.root,self.target,self.root)
        self.assertFalse((work/'supabase/migrations/002_schema.sql').exists())
    def test_two_checkouts_get_distinct_identity_and_ports(self):
        _,ports,_,_=S.prepare(self.root,self.target,self.root)
        other=Path(self.tmp.name)/'other';other.mkdir()
        target=S.location(str(other))[1]
        _,ports2,_,_=S.prepare(other,target,self.root)
        self.assertNotEqual(target,self.target)
        self.assertFalse(set(ports.values())&set(ports2.values()))
    def test_credentials_reject_remote_endpoint(self):
        with self.assertRaisesRegex(ValueError,'unexpected API'):
            S.credentials({'API_URL':'https://prod.supabase.co','ANON_KEY':'anon','SERVICE_ROLE_KEY':'secret'},['NEXT_PUBLIC_SUPABASE_URL'],54321)
    def test_remote_docker_never_used(self):
        with mock.patch.object(S,'docker_binary',return_value='/tools/docker'),mock.patch.object(S,'command',return_value=json.dumps([{'Endpoints':{'docker':{'Host':'ssh://production'}}}])) as cmd:
            with self.assertRaisesRegex(ValueError,'Remote Docker'):S.docker_env()
            self.assertEqual(cmd.call_count,1)
    def test_conventional_mapping_only(self):
        result=S.mapping(['VITE_SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY_DEV','CUSTOM_SUPABASE_API_TOKEN','NEXT_PUBLIC_SUPABASE_ENV'])
        self.assertEqual(result,{'VITE_SUPABASE_URL':'API_URL','SUPABASE_SERVICE_ROLE_KEY_DEV':'SERVICE_ROLE_KEY'})
    def test_lifecycle_and_production_override(self):
        self.write('.env','NEXT_PUBLIC_SUPABASE_URL=https://prod.supabase.co\nSUPABASE_SERVICE_ROLE_KEY=prod-secret\nSTRIPE_SECRET_KEY=unrelated\n')
        calls=[]
        def run(argv,*args,**kw):
            calls.append(list(map(str,argv)))
            if argv[1]=='status':
                port=S.read_state(self.target)['ports']['api.port']
                return json.dumps({'API_URL':f'http://127.0.0.1:{port}','ANON_KEY':'local-anon','SERVICE_ROLE_KEY':'local-secret'})
            return ''
        with mock.patch.object(S,'docker_env',return_value={'PATH':'/bin'}),mock.patch.object(S,'install_cli',return_value='/tools/supabase'),mock.patch.object(S,'command',side_effect=run):
            S.start(str(self.root));state=self.wait()
            self.assertEqual(state['status'],'ready',state)
            self.assertNotIn('local-secret',json.dumps(state))
            values=R.environment(str(self.root),validate_required=False)[1]
            self.assertEqual(values['SUPABASE_SERVICE_ROLE_KEY'],'local-secret')
            self.assertTrue(values['NEXT_PUBLIC_SUPABASE_URL'].startswith('http://127.0.0.1:'))
            self.assertEqual(values['STRIPE_SECRET_KEY'],'unrelated')
            S.start(str(self.root),action='stop');state=self.wait()
            self.assertEqual(state['status'],'stopped')
            self.assertNotIn('--no-backup',sum(calls,[]));self.assertNotIn('--linked',sum(calls,[]))
            self.assertTrue((self.target/'credentials.json').exists())
            with self.assertRaisesRegex(ValueError,'stopped'):S.launch_values(self.root)
            S.start(str(self.root));self.assertEqual(self.wait()['status'],'ready')
    def test_daemon_pause_resumes_and_duplicate_requests_do_not_install_twice(self):
        with mock.patch.object(S,'docker_env',side_effect=S.DockerUnavailable('stopped')),mock.patch.object(S,'install_docker') as install:
            S.start(str(self.root));state=self.wait()
            self.assertEqual(state['status'],'needs_input');self.assertEqual(install.call_count,1)
            self.assertTrue(state['selected'])
    def test_existing_remote_context_does_not_trigger_installation(self):
        with mock.patch.object(S,'docker_env',side_effect=ValueError('Remote Docker forbidden')),mock.patch.object(S,'install_docker') as install:
            S.start(str(self.root));state=self.wait()
            self.assertIn('Remote Docker',state['reason']);install.assert_not_called()
    def test_interrupted_operation_can_resume(self):
        S.write_state(self.target,{'status':'working','selected':True})
        self.assertEqual(S.inspect(str(self.root))['status'],'needs_input')
    def test_cross_process_lease(self):
        handle=S.lease(self.target/'operation.lock')
        try:self.assertIsNone(S.lease(self.target/'operation.lock'))
        finally:handle.close()
    def test_platform_installers_are_host_owned(self):
        with mock.patch.object(S.platform,'system',return_value='Windows'),mock.patch.object(S,'docker_binary',return_value=None),mock.patch.object(S.shutil,'which',return_value='/tools/winget'),mock.patch.object(S,'command') as run:
            S.install_docker()
            self.assertEqual(run.call_args.args[0],['/tools/winget','install','--exact','--id','Docker.DockerDesktop','--source','winget','--interactive'])

if __name__=='__main__':unittest.main()
