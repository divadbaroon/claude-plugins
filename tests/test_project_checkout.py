import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from human_compact.trajectory import project_checkout as C


class CheckoutTests(unittest.TestCase):
    def test_url_validation(self):
        self.assertEqual(C.identity('https://github.com/Owner/Repo.git/')['url'], 'https://github.com/owner/repo.git')
        self.assertEqual(C.identity('https://github.com/a/b/tree/feature%2Fcharts')['ref'], 'feature/charts')
        for value in ['https://evil.com/a/b', 'https://user:token@github.com/a/b', 'file:///tmp/repo', 'https://github.com/a/../b', 'https://github.com/a/b?token=secret', 'https://github.com/a/b/tree/main/viz']:
            with self.subTest(value=value), self.assertRaises(ValueError): C.identity(value)

    def test_real_clone_reuse_branch_discovery_and_failure_cleanup(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'HUMAN_COMPACT_HOME':temporary+'/home'}):
            origin=Path(temporary)/'origin';origin.mkdir()
            def run(*args):
                return subprocess.run(['git',*args],cwd=origin,check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
            run('init','-b','main')
            (origin/'viz').mkdir();(origin/'viz/index.html').write_text('<h1>Chart</h1>')
            (origin/'data').mkdir();(origin/'data/input.csv').write_text('value\n1\n')
            run('add','.');run('-c','user.name=Test','-c','user.email=test@example.com','commit','-m','fixture')
            run('branch','feature/charts')
            real=C.git; clones=[]
            def local_git(args,cwd=None):
                if 'clone' in args:
                    clones.append(args)
                    args=list(args);args[-2]=str(origin)
                    result=real(args,cwd)
                    real(['remote','set-url','origin','https://github.com/example/charts.git'],args[-1])
                    return result
                return real(args,cwd)
            with patch.object(C,'git',side_effect=local_git):
                first=C.discover('https://github.com/example/charts')
                root=Path(first['root'])
                self.assertEqual(first['components'][0]['path'],str(root/'viz'))
                self.assertTrue((root/'data/input.csv').exists())
                (root/'viz/index.html').write_text('User edit')
                again=C.resolve('https://github.com/Example/Charts.git')
                self.assertTrue(again['source']['reused']);self.assertEqual(len(clones),1)
                self.assertEqual((root/'viz/index.html').read_text(),'User edit')
                branch=C.resolve('https://github.com/example/charts/tree/feature%2Fcharts')
                self.assertNotEqual(branch['path'],str(root))
                self.assertEqual(real(['branch','--show-current'],branch['path']),'feature/charts')
                with self.assertRaises(ValueError):C.resolve('https://github.com/example/charts/tree/missing')
                self.assertFalse(list((Path(temporary)/'home/projects/github.com').rglob('*.lock')))
                self.assertFalse(list((Path(temporary)/'home/projects/github.com').rglob('.clone-*')))
                real(['remote','set-url','origin','https://github.com/another/repo.git'],root)
                with self.assertRaises(ValueError):C.resolve('https://github.com/example/charts')

    def test_concurrent_and_unregistered_paths_are_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {'HUMAN_COMPACT_HOME':temporary}):
            base=Path(temporary)/'projects/github.com'; (base/'example/charts').mkdir(parents=True)
            marker=base/'example/charts/user.txt';marker.write_text('preserve')
            with self.assertRaisesRegex(ValueError,'unregistered'): C.resolve('https://github.com/example/charts')
            self.assertEqual(marker.read_text(),'preserve')
            source=C.identity('https://github.com/example/charts')
            key=C.hashlib.sha256((source['url']+'\n'+source['ref']).encode()).hexdigest()
            (base/'.registry'/(key+'.lock')).mkdir()
            with patch.object(C,'git') as commands:
                with self.assertRaisesRegex(ValueError,'already being prepared'): C.resolve('https://github.com/example/charts')
                commands.assert_not_called()

    def test_local_paths_unchanged(self):
        self.assertEqual(C.resolve('/tmp/local'),{'path':'/tmp/local'})
