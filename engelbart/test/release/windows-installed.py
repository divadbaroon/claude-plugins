"""Exercise the installed Windows package, including the real managed Preview."""
import io
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile
import time
from urllib.request import urlopen
from human_compact.trajectory import starter, resources, preview
from human_compact.trajectory.agents import artifacts

assert importlib.metadata.version('human-compact') == os.environ['EXPECTED_VERSION']
assert 'site-packages' in str(Path(starter.__file__)).lower(), starter.__file__
with tempfile.TemporaryDirectory(prefix='Engelbart Windows space ') as folder:
    root=Path(folder)/'vault'
    project=Path(folder)/'project with spaces';project.mkdir()
    assert starter.prepare(root,project,[{'text':'Create a student dropdown and show their timeline'}])
    data=b'student_id,action\na,edit\nb,run\n'
    resource=resources.upload_dataset(root,project,'students.csv',io.BytesIO(data),len(data))
    assert resource['status']=='ready',resource
    (project/'app.js').write_text("""import {loadDataset,selectControl,renderTimeline} from './ui.js';
const app=document.querySelector('#app'),data=await loadDataset(),timeline=document.createElement('div');
const show=id=>timeline.replaceChildren(renderTimeline(data.rows.filter(r=>r.student_id===id),{time:'student_id',label:'action'}));
app.replaceChildren(selectControl('Student',['a','b'],show),timeline);show('a');""",encoding='utf-8')
    try:
        start=preview.show_ui(root,str(project));assert start.get('ok'),start
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            state=preview.state(root,str(project))
            if state.get('url') and state.get('status')=='running':break
            time.sleep(.2)
        else:raise AssertionError(state)
        with urlopen(state['url'].rstrip('/')+'/api/dataset') as response:
            rows=json.load(response)['rows'];assert rows[1]['action']=='run',rows
        checked=artifacts.inspect_page(state['url'],[
            {'kind':'control','role':'combobox','name':'Student'},
            {'kind':'text','text':'a — edit'},
        ])
        print('ARTIFACT_EVIDENCE '+json.dumps(checked,ensure_ascii=True),flush=True)
        assert checked['passed'],checked
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as playwright:
            browser=playwright.chromium.launch(headless=True,executable_path=artifacts.browser_executable())
            try:
                page=browser.new_page();page.goto(state['url'])
                page.get_by_role('combobox',name='Student').select_option('b')
                expect(page.get_by_text('b — run',exact=True)).to_be_visible()
            finally:
                browser.close()
        print(json.dumps({'installedVersion':importlib.metadata.version('human-compact'),'starter':True,'pathWithSpaces':True,'dataset':True,'browserCheck':True}))
    finally:
        proc=preview.running(str(project))
        preview.stop(str(project),root=root)
        stopped=True
        if proc and proc.process:
            import subprocess
            try:proc.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                stopped=False
                # Clean up only this fixture's process tree, after recording
                # failure of the real Stop operation. Never turn it into a pass.
                from human_compact.platform_compat import kill_process_tree
                kill_process_tree(proc.process.pid,force=True)
                proc.process.wait(timeout=10)
            proc.thread.join(timeout=5)
        print('STOP_EVIDENCE '+json.dumps({'stoppedNormally':stopped}),flush=True)
        assert stopped,'Preview Stop did not terminate the process within 5 seconds'
