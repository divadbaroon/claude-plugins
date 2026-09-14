"""Bounded lexical JS/TS environment extraction; never executes source.

Tokens distinguish code from comments and literal strings. This is deliberately
not a full JS data-flow analyser: only direct accesses and simple local function
parameters forwarded to an environment lookup are resolved.
"""
import re
from bisect import bisect_right

TOKEN = re.compile(r'''(?P<space>\s+)|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)|(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<template>`(?:\\.|[^`\\])*`)|(?P<word>[A-Za-z_$][\w$]*)|(?P<op>\.\.\.|\?\?|\|\||=>|\?\.|[^\s])''')
KEY = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*$')


def scan(text):
    tokens=[];warnings=[]
    newlines=[m.start() for m in re.finditer('\n',text)]
    offset=0
    while offset<len(text):
        m=TOKEN.match(text,offset)
        if not m:offset+=1;continue
        offset=m.end();kind=m.lastgroup
        if m[0]=='/' and (not tokens or tokens[-1][0] in ('=','(','[',',',':','return','=>')):
            regex=re.match(r'/((?:\\.|\[(?:\\.|[^\]\\])*\]|[^/\n\\])*)/[a-z]*',text[m.start():])
            if regex:
                offset=m.start()+len(regex[0]);continue
        if kind in ('space','comment'):continue
        if kind=='template':
            if '${' in m[0] and ('process' in m[0] or 'import.meta' in m[0]):warnings.append(('Environment access inside template expressions is not resolved',bisect_right(newlines,m.start())+1))
            continue
        tokens.append((m[0],kind,bisect_right(newlines,m.start())+1))
    if len(tokens)>100000:return [],[('JavaScript token budget exceeded',1)]
    t=[x[0] for x in tokens];findings=[];lookups=[]
    def literal(i):
        if i>=len(t) or tokens[i][1]!='string':return None
        v=t[i][1:-1]
        return v if KEY.fullmatch(v) else None
    def matching(i,left='{',right='}'):
        depth=0
        for j in range(i,len(t)):
            if t[j]==left:depth+=1
            elif t[j]==right:
                depth-=1
                if depth==0:return j
        return None
    # API contracts whose client constructors reject absent URL/key arguments.
    # Require a named import from the actual SDK, not a coincidental function
    # name or a TypeScript non-null assertion (which is not runtime validation).
    contracts={'@supabase/ssr':{'createBrowserClient','createServerClient'},
               '@supabase/supabase-js':{'createClient'}}
    constructors=set();import_positions=set()
    for k in range(len(t)-5):
        if t[k:k+2]!=['import','{']:continue
        close=matching(k+1)
        if close is None or close+2>=len(t) or t[close+1]!='from':continue
        package=t[close+2][1:-1] if tokens[close+2][1]=='string' else ''
        if package not in contracts:continue
        import_positions.update(range(k,close+3))
        pieces=[];start=k+2
        for j in range(start,close+1):
            if j==close or t[j]==',':pieces.append(t[start:j]);start=j+1
        for part in pieces:
            if len(part)==1 and part[0] in contracts[package]:constructors.add(part[0])
            elif len(part)==3 and part[0] in contracts[package] and part[1]=='as':constructors.add(part[2])
    # Decline files that redeclare an imported name; lexical shadowing is not
    # sufficiently resolved by this bounded scanner.
    constructors={name for name in constructors if not any(
        j not in import_positions and t[j]==name and (t[j-1] in ('const','let','var','function','class','(', ',') or t[j+1]=='=')
        for j in range(1,len(t)-1))}
    required_accesses=set()
    for k,name in enumerate(t):
        if name not in constructors or (k and t[k-1] in ('.','?.','function')):continue
        opening=k+1
        if opening<len(t) and t[opening]=='<':
            end_type=matching(opening,'<','>')
            if end_type is None:continue
            opening=end_type+1
        if opening>=len(t) or t[opening]!='(':continue
        closing=matching(opening,'(',')')
        if closing is None:continue
        # Only bare direct accesses (optionally !), not nested calls, fallbacks,
        # computed expressions or values elsewhere in the options object.
        cursor=opening+1
        for argument in range(2):
            end=cursor
            while end<closing and t[end]!=',':end+=1
            expr=t[cursor:end]
            if expr and expr[-1]=='!':expr=expr[:-1]
            base=3 if expr[:3]==['process','.','env'] else 5 if expr[:5]==['import','.','meta','.','env'] else 0
            if base and ((len(expr)==base+2 and expr[base]=='.' and KEY.fullmatch(expr[base+1])) or
                         (len(expr)==base+3 and expr[base]=='[' and expr[-1]==']' and literal(cursor+base+1))):
                required_accesses.add(cursor)
            cursor=end+1
            if cursor>=closing:break
    def requirement(end):
        if end+1<len(t) and t[end] in ('||','??') and (tokens[end+1][1]=='string' and t[end+1][1:-1] or t[end+1] in ('true','false') or t[end+1].isdigit()):return 'optional'
        return 'unknown'
    for i in range(len(t)):
        n=3 if t[i:i+3]==['process','.','env'] else 5 if t[i:i+5]==['import','.','meta','.','env'] else 0
        if not n:continue
        end=i+n;name=None;dynamic=None
        if end+1<len(t) and t[end] in ('.','?.') and tokens[end+1][1]=='word':name=t[end+1];end+=2
        elif end+2<len(t) and t[end]=='[' and t[end+2]==']':
            name=literal(end+1);dynamic=t[end+1] if not name else None;end+=3
        elif end<len(t) and t[end]=='[':warnings.append(('Dynamic environment access',tokens[i][2]))
        if name:
            req='required' if i in required_accesses else requirement(end)
            # Simple local binding followed by an explicit missing-value throw.
            if i>=3 and t[i-1]=='=' and tokens[i-2][1]=='word':
                var=t[i-2]
                for k in range(end,min(len(t),end+180)):
                    if t[k:k+5]==['if','(','!',var,')'] and ('throw' in t[k+5:k+8]):req='required'
            findings.append((name,tokens[i][2],'sdk-required-argument' if i in required_accesses else 'reference',req))
        if dynamic:lookups.append((i,dynamic,tokens[i][2]))
        # Object destructuring immediately preceding the accessor.
        if i>=2 and t[i-1]=='=' and t[i-2]=='}':
            start=i-3;depth=1
            while start>=0:
                if t[start]=='}':depth+=1
                if t[start]=='{':
                    depth-=1
                    if depth==0:break
                start-=1
            if start>=0:
                k=start+1
                while k<i-2:
                    if tokens[k][1]=='word' and (k==start+1 or t[k-1]==','):
                        name=t[k];j=k+1
                        if j+1<i-2 and t[j]==':':j+=2
                        req='optional' if j+1<i-2 and t[j]=='=' and tokens[j+1][1]=='string' and t[j+1][1:-1] else 'unknown'
                        findings.append((name,tokens[k][2],'destructuring',req))
                    k+=1
    if len(lookups)>32:warnings.append(('Dynamic helper analysis budget exceeded',lookups[32][2]))
    for pos,param,line in lookups[:32]:
        resolved=False
        # Only a named function declaration with an unmodified first parameter.
        for k in range(pos):
            if t[k:k+1]!=['function'] or k+3>=len(t) or t[k+2]!='(' or t[k+3]!=param:continue
            close=matching(k+2,'(',')')
            if close is None:continue
            body=next((j for j in range(close+1,min(close+12,len(t))) if t[j]=='{'),None)
            if body is None:continue
            finish=matching(body)
            if finish is None or not body<pos<finish:continue
            if any(t[j]==param and t[j+1] in ('=','++','--') for j in range(body,finish-1)):continue
            name=t[k+1]
            if sum(t[q:q+2]==['function',name] for q in range(len(t)-1))!=1:continue
            if any(t[q]==name and t[q+1]=='=' for q in range(len(t)-1)):continue
            for j in range(len(t)-3):
                if j==k+1 or body<=j<=finish:continue
                if t[j:j+2]==[name,'('] and (j==0 or t[j-1] not in ('.','?.','function')):
                    value=literal(j+2)
                    if value and t[j+3] in (')',','):
                        findings.append((value,tokens[j][2],'helper-reference','unknown'));resolved=True
        # Keep uncertainty visible: other call sites and shadowing are not proven.
        warnings.append(('Dynamic environment access'+(' (literal helper calls also found)' if resolved else ''),line))
    return findings,warnings
