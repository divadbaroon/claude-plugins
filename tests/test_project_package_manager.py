import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from human_compact.trajectory import project_package_manager as PM, project_setup as S

class PackageManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve()
    def manifest(self,manager=None):
        package={'scripts':{'dev':'next dev','build':'next build'}}
        if manager:package['packageManager']=manager
        (self.root/'package.json').write_text(json.dumps(package))
    def test_preserve_lockfile_manager_and_inherit_workspace(self):
        self.manifest();(self.root/'bun.lock').write_text('{}')
        child=self.root/'app';child.mkdir()
        self.assertEqual('bun',PM.evidence(self.root,child)['manager'])
        with self.assertRaisesRegex(ValueError,'do not substitute npm'):
            S.command(self.root,{'argv':['npm','install']},True)
        S.command(self.root,{'argv':['bun','install','--frozen-lockfile']},True)
    def test_supported_commands_and_forwarding(self):
        for manager in ('bun','pnpm','yarn'):
            self.manifest(manager+'@1.2.3')
            for argv,prep in [([manager,'install'],True),([manager,'run','build'],True),([manager,'run','dev','--port','3201','--hostname','127.0.0.1'],False)]:
                self.assertEqual(argv,S.command(self.root,{'argv':argv},prep)['argv'])
            for argv in ([manager,'install','--force'],[manager,'add','evil'],[manager,'run','dev','--hostname','0.0.0.0']):
                with self.assertRaises(ValueError):S.command(self.root,{'argv':argv},argv[1]!='run')
    def test_missing_version_and_no_implicit_corepack_download(self):
        self.manifest('pnpm@9.1.0')
        with patch.object(PM.shutil,'which',return_value=None):
            with self.assertRaisesRegex(ValueError,'not installed'):PM.check_runtime(self.root,self.root,['pnpm','install'],{})
        with patch.object(PM.shutil,'which',return_value='/tools/pnpm'),patch.object(PM.subprocess,'run',return_value=SimpleNamespace(returncode=0,stdout='9.2.0')) as run:
            with self.assertRaisesRegex(ValueError,'requires pnpm@9.1.0'):PM.check_runtime(self.root,self.root,['pnpm','install'],{})
            self.assertEqual('0',run.call_args.kwargs['env']['COREPACK_ENABLE_NETWORK'])
            run.return_value.stdout='9.1.0'
            self.assertEqual('9.1.0',PM.check_runtime(self.root,self.root,['pnpm','install'],{})['version'])
    def test_conflicting_locks(self):
        self.manifest()
        for name in ('bun.lock','yarn.lock'):(self.root/name).write_text('')
        with self.assertRaisesRegex(ValueError,'Conflicting'):PM.check_choice(self.root,self.root,'npm')

    def test_railpack_manager_is_preserved_without_lockfile(self):
        self.manifest()
        railpack={'steps':[{'name':'install','commands':[{'cmd':'bun install --frozen-lockfile'}]}]}
        plan={'preparation':[{'cwd':str(self.root),'argv':['npm','install']}],'services':[]}
        with self.assertRaisesRegex(ValueError,'Railpack selected bun'):
            PM.preserve_railpack(plan,railpack,self.root)
        plan['preparation'][0]['argv']=['bun','install','--frozen-lockfile']
        PM.preserve_railpack(plan,railpack,self.root)

    def test_managed_bun_path_and_version_validation(self):
        self.manifest('bun@1.3.14')
        with patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(self.root/'hc')}):
            directory=PM.bun_bin('1.3.14');directory.mkdir(parents=True)
            (directory/('bun.exe' if os.name=='nt' else 'bun')).write_text('fixture')
            env=PM.launch_env(self.root,self.root,{'PATH':'original'})
            self.assertEqual(str(directory)+os.pathsep+'original',env['PATH'])
            with self.assertRaises(ValueError):PM.bun_home('../../escape')
            with patch.object(PM.shutil,'which',return_value=None):
                with self.assertRaises(PM.MissingBun):PM.check_runtime(self.root,self.root,['bun','install'],{})
