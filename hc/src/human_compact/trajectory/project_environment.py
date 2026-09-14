"""Bounded, non-executing environment inventory. Never returns secret values."""
import ast
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
from human_compact.trajectory.project_env_js import scan as scan_js

KEY = r'[A-Za-z_][A-Za-z0-9_]*'
SKIP = {'.git', 'node_modules', '.next', 'dist', 'build', '.venv', 'venv', '__pycache__', '.claude', '.human-compact'}
EXT = {'.js', '.jsx', '.ts', '.tsx', '.mjs', '.cjs', '.py', '.sh'}
SCHEMAS = {'.env.schema.json', 'env.schema.json'}
EXAMPLES = {'.env.example', '.env.sample', '.env.template'}
LOCK = threading.Lock()


def project(directory):
    if not isinstance(directory, str) or not Path(directory).expanduser().is_absolute():
        raise ValueError('Enter an absolute local project path.')
    root = Path(directory).expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError('Select a directory.')
    return root


def read(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError('Not a regular local file')
    with path.open('rb') as f:
        data = f.read(512 * 1024 + 1)
    if len(data) > 512 * 1024:
        raise ValueError('File exceeds scan limit')
    return data.decode('utf-8')


def storage(root):
    home = Path(os.environ.get('HUMAN_COMPACT_HOME') or Path.home()/'.human-compact').expanduser().resolve()
    folder = home/'project-environments'
    if folder.is_symlink() or folder.resolve().is_relative_to(root):
        raise ValueError('Environment storage must be outside the repository.')
    return folder, hashlib.sha256(str(root).encode()).hexdigest()+'.json'


def saved(root):
    folder, name = storage(root)
    file = folder/name
    if not file.exists():
        return {}
    result = json.loads(read(file))
    if not isinstance(result, dict):
        raise ValueError('Invalid local environment storage')
    return result


def dotenv(text):
    result = {}
    for line in text.splitlines():
        m = re.match(r'^\s*(?:export\s+)?('+KEY+r')\s*=\s*(.*)$', line)
        if m:
            value = m[2].strip()
            if value.startswith(('"', "'")):
                quote = value[0]
                value = value[1:value.find(quote, 1)] if quote in value[1:] else '$unresolved'
            else:
                value = re.split(r'\s+#', value)[0].strip()
            result[m[1]] = value
    return result


SHELL_BUILTINS = {'HOME','PATH','PWD','OLDPWD','SHELL','USER','LOGNAME','UID','EUID','PPID','BASH_SOURCE','BASH_VERSION','BASH_VERSINFO','BASH_REMATCH','BASHOPTS','SHELLOPTS','RANDOM','SECONDS','LINENO','IFS','OPTARG','OPTIND','HOSTNAME','SHLVL','TERM','TMPDIR','TMP','TEMP'}


def shell_inputs(text):
    """Only explicit external-input idioms, not every $shell_variable."""
    lines=[];delimiter=None
    for number,line in enumerate(text.splitlines(),1):
        if delimiter:
            if line.strip()==delimiter:delimiter=None
            continue
        if line.lstrip().startswith('#'):continue
        heredoc=re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)",line)
        if heredoc:delimiter=heredoc[1]
        # Single-quoted text is literal shell content (grep/awk programs included).
        code=re.sub(r"'[^']*'", "''", line)
        lines.append((number,code))
    assigned=set()
    for _,line in lines:
        assigned.update(m[1] for m in re.finditer(r'(?:^|;)\s*(?:export\s+)?('+KEY+r')(?:\[[^]]*\])?\+?=',line))
        assigned.update(m[1] for m in re.finditer(r'\bfor\s+('+KEY+r')\s+in\b',line))
        for m in re.finditer(r'\b(?:local|declare|read)\s+([^;|]+)',line):
            assigned.update(re.findall(r'\b'+KEY+r'\b',m[1]))
    found=[]
    for number,line in lines:
        for m in re.finditer(r'\$\{('+KEY+r')(:?[-=?])',line):
            name,op=m[1],m[2]
            if name in SHELL_BUILTINS or name in assigned:continue
            tail=line[m.end():]
            requirement='required' if '?' in op else 'optional' if tail and not tail.startswith('}') else 'unknown'
            # A missing-value guard feeding a missing-input list is evidence of validation.
            if re.search(r'\[\[\s+-z\s+',line) and 'missing+=' in line: requirement='required'
            found.append((name,number,requirement))
        for m in re.finditer(r'\bprintenv\s+(?:"('+KEY+r')"|('+KEY+r'))',line):
            name=m[1] or m[2]
            if name not in SHELL_BUILTINS:found.append((name,number,'unknown'))
    return found


def repository_boundary(component, explicit=None):
    if explicit:
        root=project(explicit)
        if not component.is_relative_to(root):raise ValueError('Component must be inside the repository')
        for candidate in (component,*component.parents):
            if (candidate/'.git').is_dir() or (candidate/'.git').is_file():
                return candidate if root.is_relative_to(candidate) else root
        return root
    for candidate in (component,*component.parents):
        if (candidate/'.git').is_dir() or (candidate/'.git').is_file():return candidate
    return component


def group_inventory(root, repo, rows, inventory, package):
    """Separate source requirements from independent files, without inferring a call graph."""
    scripts=package.get('scripts',{})
    launch_commands=' '.join(str(scripts.get(k,'')) for k in ('start','dev'))
    def scope(row):
        sources=[e for e in row['evidence'] if e['kind']!='example']
        if sources and all(e['kind']=='shell-input' for e in sources):
            # Only unreferenced deployment scripts can confidently be separate tasks.
            paths=[Path(e['file']) for e in sources]
            if all(p.name.startswith(('deploy_', 'deploy-', 'deploy.')) and p.name not in launch_commands for p in paths):return 'other'
        if row.get('conflict'):return 'unresolved'
        return 'required' if row['requirement']=='required' and row.get('requirementScope')!='script' else 'optional' if row['requirement']=='optional' else 'unresolved'
    display=[]
    for row in rows.values():
        row['group']=scope(row)
        row['blocksContinuation']=row['group'] in ('required','unresolved') and row['status'] not in ('found','optional')
        row['editable']=True
        row['purpose']='Deployment task, not the selected app entry point' if row['group']=='other' else 'Required by application code; startup versus feature use is not yet distinguished' if row['group']=='required' else 'Default or optional declaration found' if row['group']=='optional' else 'Needs review for the selected app'
        display.append(row)
    for item in inventory:
        if item['name'] in rows:continue
        row={**item,'group':'other','status':'not_applicable','editable':False,'blocksContinuation':False,'purpose':'Referenced outside this component; not a requirement established for this launch','source':None,'public':False}
        # Shared code might be a transitive dependency. Do not assert irrelevance.
        if any('packages' in Path(e['file']).parts for e in item['evidence']):
            row.update(group='unresolved',purpose='Shared-package usage; applicability to this app has not been established')
        display.append(row)
    return sorted(display,key=lambda r:(('required','optional','other','unresolved').index(r['group']),r['name']))


def scan(directory, *, include_nested=False, repository_root=None):
    root = project(directory)
    rows, warnings = {}, []
    def add(name, file, line, kind, required='unknown', default=False):
        if not isinstance(name, str) or not re.fullmatch(KEY, name):
            return
        if name not in rows and len(rows) >= 500:
            if 'Variable limit reached.' not in warnings: warnings.append('Variable limit reached.')
            return
        row = rows.setdefault(name, {'name':name, 'evidence':[], 'requirement':'unknown', 'hasDefault':False})
        row['evidence'].append({'file':str(file), 'line':line, 'kind':kind, 'requirement':required})
        if kind != 'schema' and required != 'unknown':
            if row['requirement'] not in ('unknown',required): row['conflict']=True
            row['requirement']=required
            row['hasDefault'] |= required=='optional'
        if kind == 'schema':
            if row['requirement'] != 'unknown' and row['requirement'] != required:
                row['conflict'] = True
            row['requirement'] = required
            row['hasDefault'] |= default
    try:
        pkg = json.loads(read(root/'package.json'))
    except (OSError, ValueError):
        pkg = {}
    deps = {**pkg.get('dependencies', {}), **pkg.get('devDependencies', {})}
    framework = 'next' if 'next' in deps else 'vite' if 'vite' in deps else 'unknown'
    count, budget = 0, 0
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not (Path(base)/d).is_symlink())
        if not include_nested and Path(base) != root and ((Path(base)/'package.json').exists() or (Path(base)/'pyproject.toml').exists()):
            warnings.append('Separate nested project excluded: '+str(Path(base).relative_to(root)))
            dirs[:] = []
            continue
        for filename in sorted(files):
            path = Path(base)/filename
            if path.suffix not in EXT and filename not in SCHEMAS | EXAMPLES:
                continue
            count += 1
            if count > 3000 or budget > 12*1024*1024:
                warnings.append('Scan budget reached; inventory is incomplete.')
                dirs[:] = []
                break
            rel = str(path.relative_to(root))
            try:
                text = read(path); budget += len(text.encode())
            except (OSError, ValueError, UnicodeError):
                warnings.append('Could not scan: '+rel); continue
            if filename in SCHEMAS:
                try:
                    schema = json.loads(text)
                    required = schema.get('required', [])
                    for name, spec in schema.get('properties', {}).items():
                        line = next((i for i,l in enumerate(text.splitlines(),1) if '"'+name+'"' in l),1)
                        add(name,rel,line,'schema','required' if name in required else 'optional','default' in spec)
                except (ValueError, TypeError, AttributeError):
                    warnings.append('Unsupported environment schema: '+rel)
                continue
            if filename in EXAMPLES:
                for i,line in enumerate(text.splitlines(),1):
                    for name in dotenv(line): add(name,rel,i,'example')
                continue
            if path.suffix == '.sh':
                for name,line,requirement in shell_inputs(text): add(name,rel,line,'shell-input',requirement)
                continue
            if path.suffix == '.py':
                try:
                    tree = ast.parse(text)
                    for node in ast.walk(tree):
                        expr = None; requirement='unknown'
                        if isinstance(node, ast.Subscript) and ast.unparse(node.value) == 'os.environ': expr=node.slice; requirement='required'
                        if isinstance(node, ast.Call) and ast.unparse(node.func) in ('os.getenv','os.environ.get'):
                            expr=node.args[0] if node.args else None
                            default=node.args[1] if len(node.args)>1 else next((k.value for k in node.keywords if k.arg=='default'),None)
                            if isinstance(default,ast.Constant) and default.value is not None: requirement='optional'
                        if expr is not None:
                            if isinstance(expr,ast.Constant) and isinstance(expr.value,str): add(expr.value,rel,node.lineno,'reference',requirement)
                            else: warnings.append(f'Dynamic environment access: {rel}:{node.lineno}')
                except SyntaxError: warnings.append('Could not parse Python: '+rel)
                continue
            if filename.startswith(('next.config.', 'vite.config.')) and re.search(r'\b(envDir|envPrefix|loadEnv|dotenv)\b', text):
                warnings.append('Custom environment loading may change file precedence or public names: '+rel)
            if re.search(r'\b(createEnv|envalid)\b', text) or (re.search(r'(?:^|[._/-])env(?:[._/-]|$)', rel, re.I) and re.search(r'\b(z\.object|Joi\.object)\b', text)):
                warnings.append('Executable environment schema not evaluated: '+rel)
            js_findings,js_warnings=scan_js(text)
            for name,line,kind,requirement in js_findings: add(name,rel,line,kind,requirement)
            for message,line in js_warnings: warnings.append(f'{message}: {rel}:{line}')
    if include_nested:
        for row in rows.values():
            row['status']='inventory'
            row['components']=sorted({str(Path(e['file']).parent) for e in row['evidence']})
        return {'ok':True,'path':str(root),'inventoryOnly':True,'variables':sorted(rows.values(),key=lambda r:r['name']),'warnings':warnings[:100]}
    values = {k:(v,'Engelbart local storage') for k,v in saved(root).items()}
    # Production matches the current Railpack build/start plan; don't inherit hc's own credentials.
    names = ['.env.production.local','.env.local','.env.production','.env'] if framework=='next' else ['.env.production.local','.env.production','.env.local','.env']
    for filename in names:
        if not (root/filename).exists(): continue
        try:
            for name,value in dotenv(read(root/filename)).items(): values.setdefault(name,(value,filename))
        except (OSError, ValueError, UnicodeError): warnings.append('Could not read '+filename)
    for row in rows.values():
        name=row['name']; value,source=values.get(name,('',None))
        row['source']=source
        row['public']=name.startswith('NEXT_PUBLIC_') if framework=='next' else name.startswith('VITE_') if framework=='vite' else False
        if row['public'] and value:row['publicValue']=value
        row['status']='found' if value and '$' not in value else 'uncertain' if value else 'optional' if row['requirement']=='optional' or row['hasDefault'] else 'missing' if row['requirement']=='required' else 'uncertain'
        row['requirementScope']='script' if all(e['kind']=='shell-input' for e in row['evidence']) else 'source'
        if row['requirementScope']=='script' and row['status']=='missing':row['status']='uncertain'
        if row.get('conflict'): row['status']='uncertain'
        row['evidence']=row['evidence'][:40]
    repo=repository_boundary(root,repository_root)
    inventory=scan(str(repo),include_nested=True)
    nested_inventory=inventory['variables']
    warnings.extend('Repository inventory: '+w for w in inventory['warnings'])
    # Link repository examples to matching selected-component names, preserving
    # separate requirement classifications and repository-relative provenance.
    for item in nested_inventory:
        if item['name'] not in rows:continue
        for evidence in item['evidence']:
            if evidence['kind']=='example':
                linked={**evidence,'file':os.path.relpath(repo/evidence['file'],root),'scope':'repository-example'}
                if linked not in rows[item['name']]['evidence']:rows[item['name']]['evidence'].append(linked)
    example_files=sorted({e['file'] for item in nested_inventory for e in item['evidence'] if e['kind']=='example'})
    display=group_inventory(root,repo,rows,nested_inventory,pkg)
    return {'ok':True,'path':str(root),'displayVariables':display,'repositoryRoot':str(repo),'repositoryExamples':example_files,'repositoryInventory':nested_inventory,'framework':framework,'mode':'production','variables':sorted(rows.values(),key=lambda r:r['name']),
            'warnings':warnings[:100], 'limitations':'Detection is partial: ordinary shell variables are not treated as configuration; only explicit external-input idioms are included. Custom helpers, executable schemas, dynamic names and unsupported languages may need review. Example entries and references alone do not prove a variable is required. Local values are not credential-tested; interpolation is unresolved. hc credentials are not inherited. No project is started.'}


def save(directory, values):
    root = project(directory)
    if not isinstance(values, dict) or len(values)>200:
        raise ValueError('Expected a bounded set of environment values.')
    if sum(len(v.encode('utf-8')) for v in values.values() if isinstance(v,str)) > 128*1024:
        raise ValueError('Environment values exceed the local storage limit.')
    allowed = {r['name'] for r in scan(str(root))['variables']}
    if any(k not in allowed or not isinstance(v,str) or len(v)>16384 or '\x00' in v for k,v in values.items()):
        raise ValueError('Only detected environment names and bounded text values are accepted.')
    with LOCK:
        folder,name=storage(root)
        folder.mkdir(mode=0o700,parents=True,exist_ok=True)
        folder.chmod(0o700)
        data=saved(root);data.update({k:v for k,v in values.items() if v})
        fd,temp=tempfile.mkstemp(dir=folder,prefix='.env-')
        try:
            with os.fdopen(fd,'w') as f:
                json.dump(data,f)
            os.replace(temp,folder/name)
        finally:
            if os.path.exists(temp): os.unlink(temp)
    return scan(str(root))
