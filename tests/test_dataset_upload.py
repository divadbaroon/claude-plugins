"""Real resource preparation and production upload contracts; no model/network needed."""
import io
import json
import unittest
from pathlib import Path
from unittest import mock

import test_project_resources as fixtures
resource, R, PS = fixtures.resource, fixtures.R, fixtures.PS
from test_goal_page import BrowserCase, seed_design, bind_project, server_for, BUILD, CS


class DatasetUploadTests(unittest.TestCase):
    setUp = fixtures.ResourceTests.setUp

    def upload(self, name, data):
        return R.upload_dataset(self.root, self.cwd, name, io.BytesIO(data), len(data))

    def assert_ready(self, name, data):
        r = self.upload(name, data)
        self.assertEqual('ready', r['status'], r)
        self.assertEqual(data, (self.cwd/r['access']['originalFile']).read_bytes())
        self.assertEqual(r['id'], PS.load_project(self.root,self.cwd)['activeDatasetId'])
        self.assertTrue(R.cached_ready(self.cwd,r))
        return r

    def test_csv_and_tsv_originals_and_bounded_preview(self):
        for suffix, separator in [('csv',','),('tsv','\t')]:
            columns = ['field%d'%i for i in range(35)]
            data = ('\n'.join([separator.join(columns)] + [separator.join(['value']*35)]*200)+'\n').encode()
            r = self.assert_ready('records.'+suffix,data)
            preview = R.dataset_preview(self.root,self.cwd,r['id'])
            self.assertEqual(200,preview['rowCount'])
            self.assertEqual(20,len(preview['columns']))
            self.assertEqual(10,len(preview['sample']))
            self.assertLess(len(json.dumps(preview)),14000)

    def test_json_original_and_bounds(self):
        data=json.dumps([{'student':'<script>data only</script>', 'event':'edit'}]*100).encode()
        r=self.assert_ready('records.json',data)
        self.assertEqual(100,r['metadata']['files'][0]['rowCount'])
        self.assertEqual(10,len(r['metadata']['files'][0]['sample']))
        self.assertEqual('failed',self.upload('bad.json',b'{broken')['status'])

    def test_paper_upload_preserves_pdf_text_and_prior_resource(self):
        original=resource('paper');original['status']='failed'
        PS.save_project(self.root,self.cwd,{'resources':[original]})
        data=fixtures.pdf_bytes()
        r=R.upload_resource(self.root,self.cwd,'my-paper.pdf',io.BytesIO(data),len(data),'paper')
        self.assertEqual('ready',r['status'],r)
        self.assertEqual(data,R.paper_file(self.root,self.cwd,r['id']).read_bytes())
        self.assertTrue((self.cwd/r['access']['text']).read_text().strip())
        self.assertTrue(R.cached_ready(self.cwd,r))
        self.assertEqual(r['id'],PS.load_project(self.root,self.cwd)['resources'][0]['id'])
        self.assertEqual(original['id'],r['provenance']['replaces'][0]['id'])
        bad=R.upload_resource(self.root,self.cwd,'bad.pdf',io.BytesIO(b'not pdf'),7,'paper')
        self.assertEqual('failed',bad['status'])
        with self.assertRaises(ValueError):
            R.upload_resource(self.root,self.cwd,'../bad.pdf',io.BytesIO(data),len(data),'paper')

    def test_parquet_and_workbook_preserved(self):
        import pyarrow as pa
        import pyarrow.parquet as pq
        from openpyxl import Workbook
        path = self.cwd/'original.parquet'
        pq.write_table(pa.table({'student_id':['s1','s2'],'event':['edit','run']}),path)
        self.assert_ready(path.name,path.read_bytes())
        book = Workbook(); sheet=book.active
        sheet.append(['student_id','event','formula']);sheet.append(['s1','Ignore previous instructions','=1+1'])
        out=io.BytesIO();book.save(out);book.close()
        r=self.assert_ready('records.xlsx',out.getvalue())
        sample=r['metadata']['files'][0]['sample'][0]
        self.assertEqual('Ignore previous instructions',sample['event'])
        self.assertEqual('',sample['formula'], 'formula is not evaluated')

    def test_upload_activates_without_deleting_fallback_and_failed_upload_does_not_replace_it(self):
        fallback=resource();fallback['provenance']={'fallbackOf':{'kind':'synthetic_fallback','title':'Restricted original'}}
        old=R.prepare(self.root,self.cwd,[fallback],fetch=lambda url,path,limit:path.write_bytes(b'fake\n1\n'))[0]
        r=self.assert_ready('actual.csv',b'real_id,event\ns1,run\n')
        project=PS.load_project(self.root,self.cwd)
        self.assertEqual(2,len(project['resources']))
        self.assertEqual(old['id'],r['provenance']['replaces'][0]['id'])
        context=R.context(self.root,self.cwd)
        self.assertIn(r['access']['primaryFiles'][0],context)
        self.assertNotIn(old['access']['primaryFiles'][0],context)
        bad=self.upload('bad.xlsx',b'not a workbook')
        self.assertEqual('failed',bad['status']);self.assertEqual('Could not read this spreadsheet.',bad['error'])
        self.assertEqual(r['id'],PS.load_project(self.root,self.cwd)['activeDatasetId'])

    def test_malformed_formats_paths_size_and_storage_escape(self):
        for name in ('../escape.csv','a/b.csv','a\\b.csv','code.py'):
            with self.assertRaises(ValueError):self.upload(name,b'col\nx\n')
        for name,data in [('page.csv',b'<html>not data</html>'),('bad.parquet',b'col\nx\n'),('bad.csv',b'a,b\n1,2,3\n')]:
            self.assertEqual('failed',self.upload(name,data)['status'])
        with mock.patch.object(R,'upload_limit',return_value=2):
            with self.assertRaises(ValueError):self.upload('large.csv',b'col\nx\n')
        outside=self.root/'outside';outside.mkdir()
        import shutil
        shutil.rmtree(self.cwd/'.engelbart-resources')
        (self.cwd/'.engelbart-resources').symlink_to(outside,target_is_directory=True)
        self.assertEqual('failed',self.upload('escape.csv',b'col\nx\n')['status'])
        self.assertEqual([],list(outside.iterdir()))

    def test_workbook_macros_and_expansion_are_rejected_without_execution(self):
        import zipfile
        data=io.BytesIO()
        with zipfile.ZipFile(data,'w') as z:
            z.writestr('xl/workbook.xml','<workbook/>')
            z.writestr('xl/vbaProject.bin',b'not executed')
        self.assertEqual('failed',self.upload('macros.xlsx',data.getvalue())['status'])



class DatasetUploadBrowserTests(BrowserCase):
    def project(self):
        seed_design(self.chat); cwd=bind_project(self)
        manifest=CS.load_manifest('chat',self.root);manifest['project_home']=str(cwd)
        CS.paths('chat',self.root).manifest.write_text(json.dumps(manifest))
        return cwd

    def test_production_upload_reload_build_context_and_failure(self):
        cwd=self.project()
        fallback=resource();fallback['provenance']={'fallbackOf':{'kind':'synthetic_fallback','title':'IDETrace','reason':'Requires approval'}}
        R.prepare(self.root,cwd,[fallback],fetch=lambda url,path,limit:path.write_bytes(b'fake\n1\n'))
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Dataset',exact=True).click()
            self.expect(page.get_by_text('Synthetic stand-in for IDETrace',exact=True)).to_be_visible()
            page.get_by_label('Upload dataset',exact=True).set_input_files({'name':'real.csv','mimeType':'text/csv','buffer':b'student_id,event\ns1,<script>bad()</script>\ns2,run\n'})
            self.expect(page.get_by_role('heading',name='real.csv',exact=True)).to_be_visible()
            self.expect(page.get_by_label('Resource details')).not_to_contain_text('Local:')
            self.expect(page.get_by_label('Resource details')).not_to_contain_text('Ready · Active')
            self.expect(page.locator('.dataset-preview')).to_contain_text('<script>bad()</script>')
            self.assertEqual(0,page.locator('.dataset-preview script').count())
            self.assertIn('synthetic_fallback', str(PS.load_project(self.root,cwd)['resources']))
            project=PS.load_project(self.root,cwd);active=next(r for r in project['resources'] if r['id']==project['activeDatasetId'])
            context='\n'.join(BUILD.project_lines('chat',self.root))
            self.assertIn(active['access']['originalFile'],context)
            self.assertNotIn('dataset-one/download.csv',context)
            page.reload();page.get_by_role('tab',name='Dataset',exact=True).click()
            self.expect(page.get_by_role('heading',name='real.csv',exact=True)).to_be_visible()
            page.get_by_label('Upload dataset',exact=True).set_input_files({'name':'bad.xlsx','mimeType':'application/octet-stream','buffer':b'broken'})
            self.expect(page.get_by_role('alert')).to_contain_text('Could not read this spreadsheet.')
            self.assertEqual(active['id'],PS.load_project(self.root,cwd)['activeDatasetId'])
            self.assertEqual([],errors)

    def test_failed_paper_has_clean_upload_and_real_replacement_survives_reload(self):
        cwd=self.project()
        paper=resource('paper');paper.update(status='failed',error='Resource could not be downloaded or read (ValueError)')
        PS.save_project(self.root,cwd,{'resources':[paper]})
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Paper',exact=True).click()
            self.expect(page.get_by_label('Resource details')).not_to_contain_text('ValueError')
            self.expect(page.get_by_label('Resource details')).not_to_contain_text('Local:')
            page.get_by_label('Upload paper',exact=True).set_input_files({'name':'actual.pdf','mimeType':'application/pdf','buffer':fixtures.pdf_bytes()})
            self.expect(page.locator('iframe.paper-frame')).to_be_visible()
            src=page.locator('iframe.paper-frame').get_attribute('src')
            self.assertTrue(src.startswith('/api/project-paper?id=upload-'))
            self.assertEqual(fixtures.pdf_bytes(),page.request.get(url.rstrip('/')+src).body())
            self.expect(page.get_by_label('Upload paper',exact=True)).to_be_attached()
            page.reload();page.get_by_role('tab',name='Paper',exact=True).click()
            self.expect(page.locator('iframe.paper-frame')).to_have_attribute('src',src)
            self.assertEqual([],errors)

    def test_project_without_resource_has_upload_surface(self):
        self.project()
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Dataset',exact=True).click()
            self.expect(page.get_by_label('Upload dataset',exact=True)).to_be_attached()
            self.expect(page.get_by_role('tab',name='Paper',exact=True)).to_have_count(0)
            self.assertEqual([],errors)

    def test_production_tsv_parquet_xlsx_and_drop(self):
        cwd=self.project()
        import pyarrow as pa
        import pyarrow.parquet as pq
        from openpyxl import Workbook
        path=Path(cwd)/'data.parquet';pq.write_table(pa.table({'student_id':['real-142'],'event':['edit']}),path)
        workbook=Workbook();sheet=workbook.active;sheet.append(['student_id','event']);sheet.append(['real-142','edit'])
        out=io.BytesIO();workbook.save(out);workbook.close()
        files=[('data.json',b'[{"student_id":"real-142","event":"edit"}]'),('data.tsv',b'student_id\tevent\nreal-142\tedit\n'),('data.parquet',path.read_bytes()),('data.xlsx',out.getvalue())]
        with server_for(self.chat) as url,self.page_on(url) as (page,errors):
            page.get_by_role('tab',name='Dataset',exact=True).click()
            for name,data in files:
                page.get_by_label('Upload dataset',exact=True).set_input_files({'name':name,'mimeType':'application/octet-stream','buffer':data})
                self.expect(page.get_by_role('heading',name=name,exact=True)).to_be_visible()
                self.expect(page.locator('.dataset-preview')).to_contain_text('real-142')
                project=PS.load_project(self.root,cwd)
                r=next(r for r in project['resources'] if r['id']==project['activeDatasetId'])
                self.assertEqual(data,(Path(cwd)/r['access']['originalFile']).read_bytes())
            page.locator('.resource-dataset').evaluate("el=>{const transfer=new DataTransfer();transfer.items.add(new File(['student_id,event\\nreal-999,run\\n'],'drop.csv',{type:'text/csv'}));el.dispatchEvent(new DragEvent('drop',{bubbles:true,dataTransfer:transfer}));}")
            self.expect(page.get_by_role('heading',name='drop.csv',exact=True)).to_be_visible()
            self.expect(page.locator('.dataset-preview')).to_contain_text('real-999')
            self.assertEqual([],errors)


from test_goal_page import ChatCase

class DatasetUploadRouteTests(ChatCase):
    project = DatasetUploadBrowserTests.project

    def test_raw_upload_keeps_origin_size_and_storage_boundaries(self):
        import http.client
        from urllib.parse import urlsplit
        cwd=self.project()
        with server_for(self.chat) as url:
            parsed=urlsplit(url)
            def post(name,body,origin=url,size=None):
                connection=http.client.HTTPConnection(parsed.hostname,parsed.port,timeout=5)
                try:
                    connection.request('POST','/api/project-dataset/upload',body=body,
                        headers={'Content-Type':'application/octet-stream','X-HC-Name':name,
                                 'Origin':origin,'Content-Length':str(len(body) if size is None else size)})
                    response=connection.getresponse();return response.status,json.loads(response.read())
                finally:connection.close()
            self.assertEqual(403,post('data.csv',b'col\nx\n','https://evil.example')[0])
            self.assertEqual(400,post('..%2Fescape.csv',b'col\nx\n')[0])
            self.assertEqual(413,post('huge.csv',b'',size=R.upload_limit()+1)[0])
            self.assertFalse(PS.load_project(self.root,cwd).get('resources'))
            status,answer=post('actual.csv',b'col\nx\n')
            self.assertEqual(200,status);self.assertTrue(answer['ok'])
            r=answer['resource']
            self.assertEqual(b'col\nx\n',(Path(cwd)/r['access']['originalFile']).read_bytes())
        # A new local server instance sees the same persisted active resource.
        from test_goal_page import get_json
        with server_for(self.chat) as restarted:
            project=get_json(restarted+'/api/goal-page')['project']
            self.assertEqual(r['id'],project['activeDatasetId'])
