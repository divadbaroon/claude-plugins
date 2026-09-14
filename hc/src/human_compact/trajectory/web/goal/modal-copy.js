import {h} from './dom.js';

// Read text nodes rather than innerText so closed details are included. Never
// serialize input values: environment fields may contain unsaved credentials.
export function modalText(root) {
  const blocks=new Set(['DIV','SECTION','P','H2','H3','H4','PRE','DETAILS','SUMMARY','LABEL','FORM']);
  function read(node) {
    if(node.nodeType===3)return node.textContent;
    if(node.hasAttribute?.('data-copy-control'))return '';
    if(['INPUT','TEXTAREA','SELECT','BUTTON'].includes(node.tagName))return '';
    if(node.tagName==='BR')return '\n';
    const value=Array.from(node.childNodes||[]).map(read).join('');
    return blocks.has(node.tagName)?'\n'+value+'\n':value;
  }
  const path=root.querySelector('#new-project-path')?.value;
  return (read(root)+(path?'\nLocal project: '+path:'')).replace(/\n{3,}/g,'\n\n').trim();
}

export function copyModalButton() {
  return h('button',{type:'button',class:'ghost-btn','data-copy-control':'1',onclick:async e=>{
    const button=e.currentTarget,root=button.closest('[role="dialog"]');
    const content=modalText(root);
    try {
      if(navigator.clipboard?.writeText)await navigator.clipboard.writeText(content);
      else {
        const field=h('textarea',{'aria-label':'Modal text to copy',readonly:true},content);
        root.append(field);field.select();
        try {if(!document.execCommand('copy'))throw new Error('Copy unavailable');}
        finally {field.remove();button.focus();}
      }
      button.textContent='Copied';
    } catch {
      button.textContent='Copy failed — try again';
    }
  }},'Copy all');
}
