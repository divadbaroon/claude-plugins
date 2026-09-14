"""Local Railpack planning only. Never build, install, or activate a project."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

LIMIT = 2 * 1024 * 1024
TIMEOUT = 120
_LOCK = threading.Lock()
_ACTIVE = {}


def _read(path):
    with open(path, 'rb') as stream:
        data = stream.read(LIMIT + 1)
    if len(data) > LIMIT:
        raise ValueError('Railpack output exceeded the 2 MiB limit.')
    return data.decode('utf-8', errors='replace')


def analyze(directory, repository_root=None):
    if not isinstance(directory, str) or not directory.strip():
        return {'ok': False, 'error': 'Select a local project directory.'}
    try:
        path = Path(directory).expanduser()
        if not path.is_absolute():
            raise ValueError('Enter the full local project path.')
        path = path.resolve(strict=True)
        root=Path(repository_root or path).expanduser().resolve(strict=True)
        if not path.is_relative_to(root) or not root.is_dir():raise ValueError('Project must be inside the selected repository')
        if not path.is_dir():
            raise ValueError('The selected path is not a directory.')
    except (OSError, ValueError, RuntimeError) as exc:
        return {'ok': False, 'error': 'The selected directory does not exist or cannot be opened. ' + str(exc)[:500]}
    from . import project_static as PS
    if PS.eligible(path):
        native=PS.plan(path,root)
        info={'success':True,'detectedProviders':['staticfile'],'metadata':{'providers':'staticfile'},'execution':'builtin-static'}
        result={'ok':True,'name':path.name,'path':str(path),'repositoryRoot':str(root),
                'source':'native-static','plan':{},'nativePlan':native,'info':info,
                'rawPlan':json.dumps(native,indent=2),'rawInfo':json.dumps(info,indent=2),
                'stdout':native['summary'],'stderr':''}
        from . import project_run
        result['analysisId']=project_run.retain(result)
        return result
    binary = shutil.which(os.environ.get('HC_RAILPACK_BIN') or 'railpack')
    if not binary:
        return {'ok': False, 'error': 'Railpack is not installed. Install Railpack on this machine, then retry.'}
    job={'cancel':threading.Event(),'done':threading.Event()}
    with _LOCK:
        previous=_ACTIVE.get(str(path))
        if previous:previous['cancel'].set()
        _ACTIVE[str(path)]=job
    try:
        if previous and not previous['done'].wait(5):
            return {'ok':False,'error':'The previous analysis is still stopping. Retry shortly.'}
        if job['cancel'].is_set():return {'ok':False,'cancelled':True,'error':'Analysis replaced by a newer request for this project.'}

        with tempfile.TemporaryDirectory(prefix='hc-railpack-') as temporary:
            work = Path(temporary)
            plan, info = work / 'plan.json', work / 'info.json'
            out, err = work / 'stdout.txt', work / 'stderr.txt'
            command = [binary, 'prepare', '--plan-out', str(plan), '--info-out', str(info), str(path)]
            # All generated output and the process cwd are outside the source.
            # No shell, no build command, and no environment passed as --env.
            with out.open('wb') as stdout, err.open('wb') as stderr:
                process = subprocess.Popen(command, cwd=temporary, stdin=subprocess.DEVNULL,
                                           stdout=stdout, stderr=stderr)
                failure = ''
                deadline = time.monotonic() + TIMEOUT
                try:
                    while process.poll() is None:
                        if job['cancel'].is_set():
                            failure='Analysis replaced by a newer request for this project.'
                            break
                        if time.monotonic() >= deadline:
                            failure = 'Railpack analysis timed out. You can retry.'
                            break
                        if any(p.exists() and p.stat().st_size > LIMIT for p in (plan, info, out, err)):
                            failure = 'Railpack output exceeded the 2 MiB limit.'
                            break
                        time.sleep(.05)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait()
            if job['cancel'].is_set():return {'ok':False,'cancelled':True,'error':'Analysis replaced by a newer request for this project.'}
            raw_out = _read(out)
            raw_err = _read(err)
            result = {'ok': False, 'name': path.name, 'path': str(path),
                      'repositoryRoot':str(root), 'stdout': raw_out, 'stderr': raw_err, 'exitCode': process.returncode}
            if failure or process.returncode:
                result['error'] = failure or ('Railpack exited with code ' + str(process.returncode) + ':\n' + (raw_err or raw_out)[-8000:])
                return result
            raw_plan, raw_info = _read(plan), _read(info)
            result.update(ok=True, plan=json.loads(raw_plan), info=json.loads(raw_info),
                          rawPlan=raw_plan, rawInfo=raw_info)
            from . import project_run
            with _LOCK:
                if _ACTIVE.get(str(path)) is not job or job['cancel'].is_set():return {'ok':False,'cancelled':True,'error':'Analysis replaced by a newer request for this project.'}
                result['analysisId'] = project_run.retain(result)
            return result
    except (OSError, ValueError) as exc:
        return {'ok': False, 'error': 'Could not analyze the project: ' + str(exc)[:8000]}
    finally:
        with _LOCK:
            if _ACTIVE.get(str(path)) is job:_ACTIVE.pop(str(path),None)
        job['done'].set()
