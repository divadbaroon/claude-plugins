"""Observable, bounded setup inference. No inferred thinking or secret values."""
import time
import json


def call(engine, prompt, redact, observe=None):
    observe=observe or (lambda event: None)
    prompt=redact(prompt)
    started=time.time()
    model=getattr(engine,'model',None)
    event={'status':'running','phase':'Waiting for model response','startedAt':started,
           'timeoutSeconds':90,'model':model if isinstance(model,str) else 'configured model',
           'provider':getattr(engine,'kind','configured'),'effort':'low','tools':'disabled',
           'prompt':prompt,'promptChars':len(prompt)}
    if not isinstance(event['provider'],str):event['provider']='configured'
    event['effort']='low' if event['provider']=='claude' else 'provider default'
    try:
        evidence=json.loads(prompt.split('Evidence JSON:\n',1)[1])
        if isinstance(evidence.get('followupReason'),str):event['followupReason']=evidence['followupReason'][:500]
        event['suppliedFiles']=[f['path'] for f in evidence.get('requestedFiles',[]) if isinstance(f,dict) and isinstance(f.get('path'),str)]
    except (ValueError,IndexError,TypeError):pass
    observe(dict(event))
    try:
        # The same tool-free path, with explicit bounded planning effort for Claude.
        raw=engine.generate_plain(prompt,planning=True)
        event.update(status='done',phase='Model response received',response=redact(raw)[:24000],responseTruncated=len(raw)>24000)
        try:
            from . import providers
            result=providers._last_json_object(raw)
            if result.get('status') in ('read_more','plan','needs_input','unsupported'):
                event['outcome']=result['status']
            if result.get('status')=='read_more' and isinstance(result.get('files'),list):
                event['requestedFiles']=json.loads(redact(json.dumps(result['files'])))
        except (ValueError,TypeError,AttributeError):pass
        return raw
    except Exception as exc:
        error=redact(str(exc))[:1000]
        event.update(status='timed_out' if 'timed out' in error.lower() else 'error',phase='Model request failed',error=error)
        raise
    finally:
        event.update(finishedAt=time.time(),durationSeconds=round(time.time()-started,2))
        observe(dict(event))
