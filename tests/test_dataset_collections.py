import io
import json
import os
from pathlib import Path
from unittest import mock
import unittest
import test_project_resources as fixtures
from human_compact.trajectory import dataset_collections as D
R,PS=fixtures.R,fixtures.PS

class CollectionTests(unittest.TestCase):
    setUp=fixtures.ResourceTests.setUp
    def ingest(self,files,name='TutorTrace'):
        s=D.begin(self.root,self.cwd,name,[{'path':p,'size':len(v)} for p,v in files.items()])
        for p,v in files.items(): D.put(self.root,self.cwd,s['id'],p,io.BytesIO(v),len(v))
        return D.finish(self.root,self.cwd,s['id'])
    def test_nested_collection_atomic_reload_dedup_and_context(self):
        files={'observable_metrics/metrics.csv':b'event,label\nedit,1\n','query_labels/labels.json':b'[{"label":1}]','README.md':b'data notes','raw/code.py':b'raise Exception("never run")'}
        r=self.ingest(files);self.assertEqual('ready',r['status'],r)
        self.assertEqual(4,r['manifest']['fileCount']);self.assertEqual(3,r['manifest']['folderCount'])
        for p,v in files.items(): self.assertEqual(v,(self.cwd/r['access']['localPath']/p).read_bytes())
        project=PS.load_project(self.root,self.cwd);self.assertEqual(r['id'],project['activeDatasetId'])
        manifest=json.loads((self.cwd/r['access']['manifestPath']).read_text());self.assertEqual(4,len(manifest['files']))
        self.assertLess(len(R.context(self.root,self.cwd)),7500)
        self.assertEqual(r['id'],self.ingest(files)['id'])
        bad=self.ingest({'bad.csv':b'bad,data\n1,2,3\n'})
        self.assertEqual('failed',bad['status']);self.assertEqual(r['id'],PS.load_project(self.root,self.cwd)['activeDatasetId'])
    def test_partial_retry_and_duplicate_paths(self):
        s=D.begin(self.root,self.cwd,'data',[{'path':'a.csv','size':6}])
        with self.assertRaises(ValueError): D.put(self.root,self.cwd,s['id'],'a.csv',io.BytesIO(b'a'),6)
        self.assertEqual([],D.status(self.cwd,s['id'])['uploaded'])
        with self.assertRaises(ValueError): D.finish(self.root,self.cwd,s['id'])
        D.put(self.root,self.cwd,s['id'],'a.csv',io.BytesIO(b'a\n1\n2\n'),6)
        self.assertEqual('ready',D.finish(self.root,self.cwd,s['id'])['status'])
        for path in ['../a.csv','/a.csv','a/../b.csv','a\\b.csv','C:/file.csv','NUL.csv']:
            with self.assertRaises(ValueError): D.begin(self.root,self.cwd,'data',[{'path':path,'size':1}])
        for paths in [['A.csv','a.csv'],['é.csv','e\u0301.csv'],['a','a/b.csv']]:
            with self.assertRaises(ValueError): D.begin(self.root,self.cwd,'data',[{'path':p,'size':1} for p in paths])
    def test_symlink_escape_and_mixed_invalid_files(self):
        outside=self.root/'outside';outside.mkdir(parents=True)
        s=D.begin(self.root,self.cwd,'data',[{'path':'nested/a.csv','size':4}]);folder=D.location(self.cwd,s['id'])
        try: (folder/'files'/'nested').symlink_to(outside,target_is_directory=True)
        except OSError: self.skipTest('Symlinks unavailable')
        with self.assertRaises(ValueError): D.put(self.root,self.cwd,s['id'],'nested/a.csv',io.BytesIO(b'a\n1\n'),4)
        self.assertEqual([],list(outside.iterdir()))
        r=self.ingest({'a.csv':b'a\n1\n','bad.parquet':b'not parquet'})
        self.assertEqual('ready',r['status']);self.assertEqual(2,r['manifest']['fileCount'])
    def test_large_file_is_streamed_under_new_policy(self):
        size=51*1024**2
        class Stream:
            calls=0
            def read(self,n):
                assert n<=65536
                self.calls+=1
                return (b'a\n1\n' if self.calls==1 else b'1\n')*(n//(4 if self.calls==1 else 2))
        stream=Stream();s=D.begin(self.root,self.cwd,'large',[{'path':'large.csv','size':size}])
        D.put(self.root,self.cwd,s['id'],'large.csv',stream,size)
        r=D.finish(self.root,self.cwd,s['id']);self.assertEqual('ready',r['status'],r)
        self.assertGreater(stream.calls,800);self.assertEqual(size,r['manifest']['totalBytes'])
    def test_configured_policy_unicode_and_bounded_json(self):
        r=self.ingest({'nested/測定.csv':b'metric\n1\n'})
        self.assertEqual('nested/測定.csv',r['manifest']['files'][0]['path'])
        for env, files in [({'HC_DATASET_MAX_BYTES':'3'},[{'path':'a.csv','size':4}]),
                           ({'HC_DATASET_MAX_FILE_BYTES':'3'},[{'path':'a.csv','size':4}]),
                           ({'HC_DATASET_MAX_FILES':'1'},[{'path':'a.csv','size':1},{'path':'b.csv','size':1}])]:
            with mock.patch.dict(os.environ,env), self.assertRaisesRegex(ValueError,'policy'):
                D.begin(self.root,self.cwd,'over limit',files)
        sample=D.json_sample(io.StringIO('['+','.join('{"metric":1}' for _ in range(200000))+']'))
        self.assertEqual(10,len(sample))
        self.assertEqual(r['id'],PS.load_project(self.root,self.cwd)['activeDatasetId'])

    def test_hosted_upload_is_acquired_and_signed_urls_are_not_persisted(self):
        payload={'id':'hosted-dataset','kind':'dataset','name':'Uploaded with paper','status':'selected',
                 'source':{'provider':'supabase','type':'upload','uploadId':'test-import'},
                 'manifest':{'fileCount':1,'files':[{'path':'nested/metrics.csv','size':4,'downloadUrl':'https://storage.example/file?token=private-token'}]},
                 'provenance':{'selectedBy':'paper-step'}}
        calls=[]
        def fetch(url,path,limit):
            calls.append(url);path.write_bytes(b'a\n1\n')
        resource=R.prepare(self.root,self.cwd,[payload],fetch=fetch)[0]
        self.assertEqual('ready',resource['status'],resource)
        self.assertEqual('hosted-dataset',PS.load_project(self.root,self.cwd)['activeDatasetId'])
        self.assertEqual(b'a\n1\n',(self.cwd/resource['access']['localPath']/'nested/metrics.csv').read_bytes())
        self.assertNotIn('private-token',json.dumps(PS.load_project(self.root,self.cwd)))
        R.prepare(self.root,self.cwd,[payload],fetch=fetch)
        self.assertEqual(1,len(calls))

    def test_remote_folder_acquisition_and_needs_user_replacement(self):
        files={'metrics.csv':b'metric\n1\n','nested/labels.csv':b'label\nyes\n'}
        source={'provider':'github','type':'github','repo':'org/research-repo','ref':'main','commit':'a'*40,'rootPath':'dataset','url':'https://github.com/org/research-repo/tree/main/dataset'}
        r={'id':'dataset-remote','kind':'dataset','name':'Research data','status':'selected','source':source,'manifest':{'fileCount':2,'files':[{'path':p,'size':len(v)} for p,v in files.items()]}}
        calls=[]
        def fetch(url,path,limit):
            calls.append(url); path.write_bytes(files[url.split('/dataset/')[1]])
        ready=R.prepare(self.root,self.cwd,[r],fetch=fetch)[0];self.assertEqual('ready',ready['status'],ready)
        self.assertEqual(2,len(calls));self.assertEqual('dataset',ready['source']['rootPath'])
        self.assertEqual(ready['id'],PS.load_project(self.root,self.cwd)['activeDatasetId'])
        blocked={**r,'id':'dataset-blocked','status':'needs_user','error':'Provider requires approval'}
        R.prepare(self.root,self.cwd,[blocked],fetch=fetch)
        uploaded=self.ingest(files,'User download')
        self.assertTrue(any(x['source'].get('repo')=='org/research-repo' for x in uploaded['provenance']['replaces']))

import test_dataset_upload as upload_fixtures
from test_goal_page import server_for, BrowserCase
class CollectionBrowserTests(BrowserCase):
    project=upload_fixtures.DatasetUploadBrowserTests.project
    # Only collection scenarios here; the inherited single-file suite is run separately.
    def test_choose_folder_and_drag_entry_tree_in_three_engines(self):
        cwd=self.project();folder=cwd/'TutorTrace';(folder/'nested').mkdir(parents=True)
        (folder/'metrics.csv').write_text('metric,label\n1,yes\n')
        (folder/'nested'/'labels.json').write_text('[{"label":"yes"}]')
        (folder/'README.md').write_text('Research data')
        with server_for(self.chat) as url,self.sync_playwright() as runtime:
            for name in ('chromium','firefox','webkit'):
                with self.subTest(browser=name):
                    browser=getattr(runtime,name).launch(headless=True)
                    try:
                        page=browser.new_page();page.goto(url)
                        page.get_by_role('tab',name='Dataset',exact=True).click()
                        fallback=page.evaluate('''async () => {
                          const {droppedFiles}=await import('/goal/dataset-files.js');
                          let message='';
                          try { await droppedFiles({items:[{kind:'file'}],files:[]}); }
                          catch(error) { message=error.message; }
                          const one=await droppedFiles({files:[new File(['a\\n1\\n'],'single.csv')]});
                          return {message,path:one[0].path};
                        }''')
                        self.assertIn('Choose folder',fallback['message'])
                        self.assertEqual('single.csv',fallback['path'])
                        race=page.evaluate('''async () => {
                          const {createActions}=await import('/goal/actions.js');
                          const {initialState}=await import('/goal/store.js');
                          const old={id:'old',kind:'dataset'}, ready={id:'new',kind:'dataset',status:'ready'};
                          let state={...initialState(),resourceId:'old',project:{resources:[old],activeDatasetId:'old'}};
                          const pending=[];
                          const actions=createActions({get:()=>state,set:patch=>{state=typeof patch==='function'?patch(state):{...state,...patch};}}, {
                            uploadCollection:async()=>({ok:true,resource:ready}),
                            loadGoal:()=>new Promise(resolve=>pending.push(resolve)),
                          });
                          const uploaded=actions.uploadDataset(new File(['a\\n1\\n'],'new.csv'));
                          await Promise.resolve();
                          const feed=actions.refresh();
                          pending[0]({project:state.project});
                          await uploaded;
                          const selectedWhilePending=state.resourceId;
                          pending[1]({project:{resources:[old,ready],activeDatasetId:'new'}});
                          await feed;
                          return {selectedWhilePending,selected:state.resourceId,active:state.project.activeDatasetId};
                        }''')
                        self.assertEqual({'selectedWhilePending':'new','selected':'new','active':'new'},race)
                        page.get_by_label('Choose dataset folder',exact=True).set_input_files(str(folder))
                        self.expect(page.get_by_role('heading',name='TutorTrace',exact=True)).to_be_visible()
                        self.expect(page.get_by_label('Resource details')).to_contain_text('3 files')
                        page.reload();page.get_by_role('tab',name='Dataset',exact=True).click()
                        self.expect(page.get_by_label('Resource details')).to_contain_text('nested/labels.json')
                        page.evaluate('''() => {
                          const file={name:'events.csv',isFile:true,file:ok=>ok(new File(['event\\nedit\\n'],'events.csv'))};
                          const directory={name:'nested',isDirectory:true,createReader:()=>{let done=false;return {readEntries:ok=>{ok(done?[]:[file]);done=true;}}}};
                          const root={name:'Dropped',isDirectory:true,createReader:()=>{let done=false;return {readEntries:ok=>{ok(done?[]:[directory]);done=true;}}}};
                          const event=new Event('drop',{bubbles:true,cancelable:true});
                          Object.defineProperty(event,'dataTransfer',{value:{items:[{kind:'file',webkitGetAsEntry:()=>root}]}});
                          document.querySelector('.resource-dataset').dispatchEvent(event);
                        }''')
                        self.expect(page.get_by_role('heading',name='Dropped',exact=True)).to_be_visible()
                        project=PS.load_project(self.root,cwd)
                        r=next(r for r in project['resources'] if r['id']==project['activeDatasetId'])
                        self.assertTrue((cwd/r['access']['localPath']/'nested'/'events.csv').exists())
                    finally: browser.close()
