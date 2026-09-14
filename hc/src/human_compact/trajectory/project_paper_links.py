"""Extract research resource URLs locally; never follow links or execute documents."""
import io
import json
import re
import sys
from urllib.parse import urlsplit, urlunsplit

LIMIT = 20 * 1024 * 1024

def extract(data, name):
    if not data or len(data)>LIMIT:raise ValueError('Choose a nonempty file under 20 MB.')
    chunks=[]
    if name.lower().endswith('.pdf'):
        from pypdf import PdfReader
        reader=PdfReader(io.BytesIO(data))
        if reader.is_encrypted:raise ValueError('Password-protected PDFs are not supported.')
        if len(reader.pages)>500:raise ValueError('Choose a PDF with at most 500 pages.')
        for page in reader.pages:
            chunks.append(page.extract_text() or '')
            for ref in page.get('/Annots',[]):
                annotation=ref.get_object()
                action=annotation.get('/A')
                if action:
                    action=action.get_object()
                    if action.get('/URI'):chunks.append(str(action['/URI']))
    elif name.lower().endswith(('.txt','.md')):chunks=[data.decode('utf-8',errors='replace')]
    elif name.lower().endswith('.docx'):
        import zipfile
        from defusedxml import ElementTree
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            for info in archive.infolist():
                if info.filename=='word/document.xml' or info.filename.startswith('word/') and info.filename.endswith('.rels'):
                    if info.file_size>LIMIT:raise ValueError('Document contents are too large.')
                    root=ElementTree.fromstring(archive.read(info))
                    chunks.extend(root.itertext())
                    chunks.extend(e.attrib['Target'] for e in root.iter() if 'Target' in e.attrib)
    else:raise ValueError('Link extraction supports PDF, DOCX, TXT, and Markdown files.')
    links={}
    for chunk in chunks:
        for raw in re.findall(r'(?:https?://)?(?:www\.)?(?:github\.com|huggingface\.co|(?:[a-z0-9-]+\.)?osf\.io)/[^\s<>"\x00]+',chunk,re.I):
            raw=raw.rstrip('.,;:)]}')
            url=urlsplit(raw if raw.startswith(('http://','https://')) else 'https://'+raw)
            host=(url.hostname or '').lower().removeprefix('www.')
            if host not in ('github.com','huggingface.co','osf.io') and not host.endswith('.osf.io'):continue
            if url.username or url.password:continue
            normalized=urlunsplit(('https',host,url.path.rstrip('/'),url.query,url.fragment))
            links[normalized]={'url':normalized,'platform':'GitHub' if host=='github.com' else 'Hugging Face' if host=='huggingface.co' else 'OSF'}
    return {'ok':True,'links':list(links.values())[:200]}

if __name__=='__main__':
    try:print(json.dumps(extract(sys.stdin.buffer.read(LIMIT+1),sys.argv[1])))
    except Exception as exc:print(json.dumps({'ok':False,'error':str(exc)[:200]}))
