import {h} from './dom.js';
export function renderAutoContinue(s,actions){
  return h('label',{class:'project-auto-continue'},
    h('input',{type:'checkbox',checked:s.autoContinue||null,onchange:e=>actions.setProjectAutoContinue(e.target.checked)}),
    ' Automatically continue and run when ready',
    h('small',{},' Checks every component’s environment. Pauses for missing configuration, approvals, or errors.'));
}
