/* File handles and paths only. Dataset bytes are streamed one file at a time. */
const junk = name => name === '.DS_Store' || name === 'Thumbs.db';
export function selectedFiles(list) {
  return Array.from(list || []).filter(f => !junk(f.name)).map(file => ({file, path:file.webkitRelativePath || file.name}));
}
export async function droppedFiles(transfer) {
  // Capture entries synchronously: browsers clear the drag data store after drop.
  const items = Array.from(transfer?.items || []);
  const entries = items.map(i => i.webkitGetAsEntry?.()).filter(Boolean);
  if (!entries.length) {
    if (items.some(i=>i.kind==='file') && !transfer?.files?.length) throw new Error('This browser cannot read dropped folders. Use Choose folder.');
    return selectedFiles(transfer?.files);
  }
  const out=[];
  async function walk(entry,prefix='') {
    if (junk(entry.name)) return;
    const path=prefix+entry.name;
    if (entry.isFile) {
      const file=await new Promise((resolve,reject)=>entry.file(resolve,reject));out.push({file,path});
      if(out.length>5000) throw new Error('Folder has too many files for browser enumeration. Import a smaller folder.');
    } else if(entry.isDirectory) {
      const reader=entry.createReader();
      while(true) {
        const batch=await new Promise((resolve,reject)=>reader.readEntries(resolve,reject));
        if(!batch.length)break;
        for(const child of batch) await walk(child,path+'/');
      }
    } else throw new Error('Unsupported folder entry; choose the folder instead.');
  }
  for(const entry of entries) await walk(entry);
  return out;
}
