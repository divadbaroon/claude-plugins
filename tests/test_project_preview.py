import unittest
from types import SimpleNamespace
from unittest.mock import patch
from human_compact.trajectory import project_run as R

class ProjectPreviewTests(unittest.TestCase):
    def test_owned_service_addresses_and_health(self):
        def proc(url, alive=True):
            return SimpleNamespace(url=url,healthy=True,embeddable=False,
                alive=lambda:alive,logs=lambda:{},process=SimpleNamespace(pid=1),started_at=1)
        main=proc('http://127.0.0.1:6000/')
        remote=proc('http://127.0.0.1:6001/',False)
        job={'state':{'status':'running','healthy':True,'stages':[{}]},'index':0,
             'proc':main,'setupProcs':{'remote':remote,'host':main}}
        with patch.dict(R._JOBS,{'preview-test':job}):
            result=R._view('preview-test')['previewServices']
        self.assertEqual(result,[
            {'id':'remote','url':remote.url,'healthy':False,'isEntry':False,'embeddable':False},
            {'id':'host','url':main.url,'healthy':True,'isEntry':True,'embeddable':False}])
        self.assertNotIn('previewServices',job['state'])
