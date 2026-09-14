import json
import os
from pathlib import Path
import select
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from human_compact.trajectory import project_run as R, project_owner as O


class OwnerTests(unittest.TestCase):
    def test_two_real_supervisors_join_state_stop_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'HUMAN_COMPACT_HOME':temporary+'/home'}):
            root=Path(temporary)/'repo';root.mkdir();(root/'index.html').write_text('owned fixture')
            chat=Path(temporary)/'chat';chat.mkdir()
            code="""
from pathlib import Path
from human_compact.trajectory import ui
s=ui.ThreadingHTTPServer(('127.0.0.1',0),ui.H)
ui._configure_server(s,Path(__import__('sys').argv[1]),True,follow=False)
print(s.server_address[1],flush=True)
s.serve_forever()
"""
            env={**os.environ,'PYTHONPATH':str(Path(__file__).resolve().parents[1]/'hc/src'),'HC_AUTOSYNC_SECONDS':'0'}
            proc=subprocess.Popen([sys.executable,'-u','-c',code,str(chat)],env=env,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
            id=None
            try:
                self.assertTrue(select.select([proc.stdout],[],[],15)[0],'Supervisor failed to start')
                port=int(proc.stdout.readline());base=f'http://127.0.0.1:{port}'
                import urllib.request
                def post(op,**args):
                    req=urllib.request.Request(base+'/api/op',data=json.dumps({'op':op,**args}).encode(),headers={'Content-Type':'application/json','Origin':base})
                    return json.load(urllib.request.urlopen(req,timeout=25))
                from human_compact.trajectory import project_static as S
                id=R.retain({'path':str(root),'plan':{},'nativePlan':S.plan(root,root)})
                self.assertTrue(post('project_run_start',id=id)['ok'])
                end=time.monotonic()+12
                while time.monotonic()<end:
                    state=R.view(id)
                    if state['status']=='running':break
                    time.sleep(.1)
                self.assertEqual('running',state['status'],state)
                self.assertNotIn(id,R._JOBS)
                blocked=R.retain({'path':str(Path(temporary)),'plan':{}})
                blockers=R.conflict_runs(blocked,[{'healthUrl':state['url']}])
                self.assertEqual([id],[item['id'] for item in blockers])
                self.assertEqual(str(root),blockers[0]['cwd'])

                other=R.retain({'path':str(root),'plan':{},'nativePlan':S.plan(root,root)})
                joined=R.start(other)
                self.assertTrue(joined['joinedExistingRun']);self.assertEqual(id,joined['id'])
                self.assertEqual(state['pid'],joined['pid']);self.assertNotIn(other,R._JOBS)
                rejected=post('project_owner_control',id=id,action='reset',token='wrong')
                self.assertFalse(rejected['ok']);self.assertEqual('running',R.view(id)['status'])
                self.assertTrue(R.reset(id)['ok']);self.assertEqual('failed',R.view(id)['status'])
                R.start(other)
                end=time.monotonic()+12
                while time.monotonic()<end:
                    restarted=R.view(other)
                    if restarted['status'] in ('running','failed','needs_input'):break
                    time.sleep(.1)
                self.assertEqual('running',restarted['status'],restarted)
                self.assertNotEqual(state['pid'],restarted['pid'])
                R.reset(other)
            finally:
                if id:
                    try:R.reset(id)
                    except Exception:pass
                for own in list(R._JOBS):R.reset(own)
                R._JOBS.clear();proc.terminate();proc.wait(timeout=10);proc.stdout.close()

    def test_unreachable_owner_does_not_launch_replacement(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ,{'HUMAN_COMPACT_HOME':temporary}):
            root=Path(temporary)/'repo';root.mkdir()
            id=R.retain({'path':str(root),'plan':{}})
            record=R.read(id);record.update(owner=O.ticket(1),run={'id':id,'cwd':str(root),'status':'running'})
            R.write(id,record)
            self.assertEqual('needs_input',R.view(id)['status'])
            other=R.retain({'path':str(root),'plan':{}})
            with self.assertRaisesRegex(ValueError,'unavailable'):R.start(other)
            self.assertNotIn(other,R._JOBS)
