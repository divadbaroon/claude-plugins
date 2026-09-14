"""Resolve GitHub sources to reusable local checkouts before component discovery."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit, unquote


def identity(value):
    url = urlsplit(value.strip())
    if url.scheme != 'https' or url.netloc.lower() != 'github.com' or url.query or url.fragment:
        raise ValueError('Use an HTTPS GitHub repository URL without credentials, query, or fragment.')
    parts = url.path.strip('/').split('/')
    if len(parts) not in (2, 4) or (len(parts) == 4 and parts[2] != 'tree'):
        raise ValueError('Use https://github.com/owner/repo or /tree/branch. Encode slashes in branch names as %2F; subdirectory URLs are not supported.')
    owner, repo = parts[:2]
    repo = repo.removesuffix('.git')
    if any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', x) or x in ('.', '..') for x in (owner, repo)):
        raise ValueError('Invalid GitHub repository name.')
    ref = unquote(parts[3]) if len(parts) == 4 else ''
    if ref and (ref.startswith('-') or not re.fullmatch(r'[A-Za-z0-9_./-]+', ref) or '..' in ref or ref.endswith('/')):
        raise ValueError('Invalid GitHub branch or tag.')
    return {'url':f'https://github.com/{owner.lower()}/{repo.lower()}.git', 'owner':owner.lower(), 'repo':repo.lower(), 'ref':ref}


def git(args, cwd=None):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    try:
        result = subprocess.run(['git', *args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=180)
    except subprocess.TimeoutExpired:
        raise ValueError('GitHub checkout timed out. Retry the URL.') from None
    except FileNotFoundError:
        raise ValueError('Git is not installed on this machine.') from None
    if result.returncode:
        raise ValueError('Could not open the GitHub repository. Check the URL, branch, network, and local Git credentials for private repositories.')
    return result.stdout.decode('utf-8', errors='replace').strip()


def resolve(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Enter a local directory or GitHub URL.')
    value = value.strip()
    if '://' not in value and not value.startswith('git@'):
        return {'path':value}
    source = identity(value)
    home = Path(os.environ.get('HUMAN_COMPACT_HOME') or Path.home()/'.human-compact')
    base = home/'projects'/'github.com'
    base.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256((source['url']+'\n'+source['ref']).encode()).hexdigest()
    registry = base/'.registry'
    registry.mkdir(exist_ok=True)
    lock = registry/(key+'.lock')
    try:
        lock.mkdir()
    except FileExistsError:
        raise ValueError('This repository is already being prepared. Retry when that operation finishes.') from None
    temporary = None
    try:
        parent = base/source['owner']
        parent.mkdir(exist_ok=True)
        target = parent/(source['repo'] + ('--ref-'+key[:12] if source['ref'] else ''))
        if parent.is_symlink() or target.is_symlink():
            raise ValueError('Managed checkout path must not be a symbolic link.')
        record = registry/(key+'.json')
        reused = target.exists()
        if reused:
            if not record.is_file() or json.loads(record.read_text()) != source:
                raise ValueError('The managed project directory is occupied by an unregistered checkout; it was left unchanged.')
            if git(['rev-parse', '--show-toplevel'], target) != str(target.resolve()) or git(['remote', 'get-url', 'origin'], target) != source['url']:
                raise ValueError('The managed checkout no longer matches its registered GitHub source; it was left unchanged.')
        else:
            temporary = Path(tempfile.mkdtemp(prefix='.clone-', dir=parent))
            args = ['-c', 'core.hooksPath='+os.devnull, 'clone', '--no-recurse-submodules']
            if source['ref']:
                args += ['--branch', source['ref'], '--single-branch']
            git([*args, '--', source['url'], str(temporary/'repo')])
            # Publish only a complete checkout. Existing work is never pulled or reset.
            (temporary/'repo').rename(target)
            record.write_text(json.dumps(source))
        return {'path':str(target.resolve()), 'source':{**source, 'reused':reused}}
    finally:
        if temporary:
            shutil.rmtree(temporary)
        lock.rmdir()


def discover(value):
    from . import project_components
    checkout = resolve(value)
    result = project_components.discover(checkout['path'])
    if 'source' in checkout:
        result['checkout'] = checkout['source']
    return result
