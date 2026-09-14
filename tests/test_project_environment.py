import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from human_compact.trajectory import project_environment as PE

class EnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)/'repo';self.root.mkdir()
        patch=mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(Path(self.tmp.name)/'hc')})
        patch.start();self.addCleanup(patch.stop)
    def write(self,name,text):
        p=self.root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text)
    def test_combined_evidence_local_values_and_no_secrets(self):
        self.write('package.json','{"dependencies":{"next":"15"}}')
        self.write('env.schema.json',json.dumps({'required':['NEEDED'],'properties':{'NEEDED':{'type':'string'},'OPTIONAL':{'type':'string','default':'secret-default'}}}))
        self.write('.env.example','CANDIDATE=example-secret\nNEEDED=\n')
        self.write('app.ts','const key=process.env.NEEDED;\nconst x=process.env["CANDIDATE"];\nconst {DESTRUCTURED: alias}=process.env;\nconst dynamic=process.env[prefix+"_KEY"];')
        self.write('.env.production.local','NEEDED=actual-secret\n')
        self.write('.env.local','NEEDED=lower-priority-secret\n')
        before={str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result=PE.scan(str(self.root));rows={r['name']:r for r in result['variables']}
        self.assertEqual('found',rows['NEEDED']['status']);self.assertEqual('.env.production.local',rows['NEEDED']['source'])
        self.assertEqual('optional',rows['OPTIONAL']['status']);self.assertEqual('uncertain',rows['CANDIDATE']['status'])
        self.assertIn('DESTRUCTURED',rows);self.assertTrue(any('Dynamic' in w for w in result['warnings']))
        self.assertNotIn('secret',json.dumps(result));self.assertEqual(before,{str(p):p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
    def test_public_values_are_editable_without_exposing_secrets(self):
        self.write('package.json','{"dependencies":{"next":"15"}}')
        self.write('app.ts','const url=process.env.NEXT_PUBLIC_SUPABASE_URL; const key=process.env.SECRET_KEY;')
        PE.save(str(self.root),{'NEXT_PUBLIC_SUPABASE_URL':'https://first.example','SECRET_KEY':'private-value'})
        result=PE.scan(str(self.root));rows={r['name']:r for r in result['displayVariables']}
        self.assertEqual(rows['NEXT_PUBLIC_SUPABASE_URL']['publicValue'],'https://first.example')
        self.assertNotIn('publicValue',rows['SECRET_KEY'])
        self.assertNotIn('private-value',json.dumps(result))
        result=PE.save(str(self.root),{'NEXT_PUBLIC_SUPABASE_URL':'https://second.example'})
        self.assertEqual(next(r for r in result['variables'] if r['name']=='NEXT_PUBLIC_SUPABASE_URL')['publicValue'],'https://second.example')

    def test_python_and_exclusions(self):
        self.write('main.py','import os\nx=os.environ["FIRST"]\ny=os.getenv("SECOND")\nz=os.environ.get(key)\n')
        self.write('node_modules/x.js','process.env.IGNORE')
        self.write('.next/x.js','process.env.IGNORE')
        self.write('nested/package.json','{}');self.write('nested/x.js','process.env.NESTED')
        result=PE.scan(str(self.root))
        self.assertEqual(['FIRST','SECOND'],[r['name'] for r in result['variables']])
        self.assertTrue(any('nested' in w for w in result['warnings']))
    def test_missing_save_is_private_atomic_and_project_scoped(self):
        self.write('env.schema.json','{"required":["TOKEN"],"properties":{"TOKEN":{"type":"string"}}}')
        self.assertEqual('missing',PE.scan(str(self.root))['variables'][0]['status'])
        result=PE.save(str(self.root),{'TOKEN':'secret-token'})
        self.assertEqual('found',result['variables'][0]['status']);self.assertNotIn('secret-token',json.dumps(result))
        folder,name=PE.storage(self.root.resolve())
        self.assertEqual(0o600,(folder/name).stat().st_mode&0o777)
        self.assertEqual(0o700,folder.stat().st_mode&0o777)
        self.assertEqual({'TOKEN':'secret-token'},PE.saved(self.root.resolve()))
        self.assertEqual(['env.schema.json'],[p.name for p in self.root.iterdir()])
        with self.assertRaises(ValueError):PE.save(str(self.root),{'UNKNOWN':'value'})
        other=Path(self.tmp.name)/'other';other.mkdir();self.assertEqual({},PE.saved(other.resolve()))
    def test_empty_precedence_and_interpolation(self):
        self.write('package.json','{"dependencies":{"vite":"6"}}');self.write('src.ts','import.meta.env.VITE_TOKEN;process.env.EMPTY;')
        self.write('.env.production','VITE_TOKEN=$OTHER\nEMPTY=\n');self.write('.env.local','EMPTY=lower\n')
        rows={r['name']:r for r in PE.scan(str(self.root))['variables']}
        self.assertEqual('uncertain',rows['EMPTY']['status']);self.assertEqual('uncertain',rows['VITE_TOKEN']['status']);self.assertTrue(rows['VITE_TOKEN']['public'])
    def test_symlinks_not_read_and_storage_inside_repo_refused(self):
        outside=Path(self.tmp.name)/'outside';outside.write_text('process.env.OUTSIDE')
        (self.root/'link.js').symlink_to(outside)
        self.assertEqual([],PE.scan(str(self.root))['variables'])
        with mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':str(self.root/'private')}):
            with self.assertRaises(ValueError): PE.save(str(self.root),{})
    def test_local_http_save_never_snapshots_values(self):
        from test_goal_page import server_for,post_json,ui
        chat=Path(self.tmp.name)/'chat';chat.mkdir()
        self.write('main.js','process.env.SECRET')
        with server_for(chat) as url,mock.patch.object(ui.H,'_note_request') as note:
            result=post_json(url+'/api/op',{'op':'save_project_environment','path':str(self.root),'values':{'SECRET':'never-log-me'}})
            self.assertTrue(result['ok']);note.assert_not_called();self.assertNotIn('never-log-me',json.dumps(result))

class ShellClassificationTests(unittest.TestCase):
    def test_locals_builtins_literals_and_heredocs_are_not_configuration(self):
        text='''#!/bin/bash
DIR="$HOME/work"
WHISPER_DIR="$HOME/whisper.cpp"
for file in *.txt; do echo "$file"; done
read -r INPUT
local tmpd
ROOT="${BASH_SOURCE[0]}"
echo "$DIR $INPUT $tmpd $WHISPER_DIR"
echo '${NOT_REAL:?no}'
python - <<'PYCODE'
print('${ALSO_NOT_REAL:?no}')
PYCODE
: "${API_TOKEN:?Required}"
printf '%s' "${OPTIONAL_ENDPOINT:-localhost}"
printenv EXTERNAL_KEY
'''
        found=PE.shell_inputs(text)
        self.assertEqual(['API_TOKEN','OPTIONAL_ENDPOINT','EXTERNAL_KEY'],[r[0] for r in found])
        self.assertEqual(['required','optional','unknown'],[r[2] for r in found])
    def test_generic_zod_schema_does_not_warn_about_environment(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as home:
            p=Path(directory);(p/'contracts.ts').write_text('const Person=z.object({name:z.string()});')
            with mock.patch.dict(os.environ,{'HUMAN_COMPACT_HOME':home}):
                self.assertFalse(PE.scan(directory)['warnings'])
