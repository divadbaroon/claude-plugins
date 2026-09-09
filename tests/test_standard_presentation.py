"""Standard presentation: verified notifications, safe Markdown and upload affordances."""
import base64
import json
import shutil
import subprocess
import unittest
from unittest import mock

from test_goal_page import BrowserCase, GOAL_DIR, seed_design, server_for, AGENT_EVENTS, GM, ui, CHAT_AGENT
from human_compact.trajectory.agents import runtime as RT


class BuildNotificationTests(unittest.TestCase):
    def test_failed_insert_retries_before_its_dependent_edits(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        store_uri = "data:text/javascript;base64," + base64.b64encode((GOAL_DIR / "store.js").read_bytes()).decode()
        editor_source = (GOAL_DIR / "todo-editor.js").read_text().replace('"./store.js"', json.dumps(store_uri))
        editor_uri = "data:text/javascript;base64," + base64.b64encode(editor_source.encode()).decode()
        source = f"import {{createTodoEditor}} from {json.dumps(editor_uri)};import {{EMPTY_SLICE,withSlice}} from {json.dumps(store_uri)};\n"
        source += """
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
if(!globalThis.crypto)globalThis.crypto=webcrypto;globalThis.document={querySelector:()=>null};
let state={activeId:'s',slices:{s:{...EMPTY_SLICE,todos:[{id:'old',text:'First second',done:false,status:'',depth:0}]}},phases:{}};
const store={get:()=>state,set:p=>{state=typeof p==='function'?p(state):{...state,...p}}};
let offline=true;const calls=[],disk=new Map([['old','First second']]);
const services={insertTodo:async({todo})=>{calls.push('insert');if(offline)throw Error('offline');disk.set(todo.id,todo.text)},
 updateTodo:async({todoId,patch})=>{calls.push('update');if(!disk.has(todoId))throw Error('missing row');disk.set(todoId,patch.text)}};
const editor=createTodoEditor({...store,changeSlice:(id,p)=>store.set(s=>withSlice(s,id,p)),services,refresh:()=>{},invalidateReads:()=>{}});
editor.todoKey({key:'Enter',target:{selectionStart:5,selectionEnd:5},preventDefault:()=>{}},'old');
const made=state.slices.s.todos[1];editor.editTodo(made.id,'Second modified');
await assert.rejects(editor.flush());assert.deepEqual(calls,['insert']);
offline=false;await editor.retrySave();
assert.deepEqual(calls,['insert','insert','update','update']);
assert.equal(disk.get('old'),'First');assert.equal(disk.get(made.id),'Second modified');
assert.equal(state.slices.s.saveError,'');await editor.flush();
"""
        run = subprocess.run([node, "--input-type=module", "-"], input=source, text=True, capture_output=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr)

    def run_js(self, body):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        source = (GOAL_DIR / "notifications.js").read_bytes()
        uri = "data:text/javascript;base64," + base64.b64encode(source).decode()
        prelude = f"import {{createNotificationActions, notificationHref}} from {json.dumps(uri)};\n"
        prelude += """
import assert from 'node:assert/strict';
const data = new Map();
const storage = {getItem:k=>data.get(k) || null, setItem:(k,v)=>data.set(k,v)};
const makeStore = () => {
  let state = {notificationScope:{project:'/research',session:'chat-one'}, phases:{},
    goal:{id:'root-one',title:'Root one'}, subgoals:[{id:'sub-one',title:'Child one'}]};
  return {get:()=>state,set:patch=>{state={...state,...patch};}};
};
const phase = (status, run='2026-09-09T10:00:00Z') => ({status, startedAt:run,
  at:'2026-09-09T10:02:00Z',todoIds:['todo-one','todo-two'],goalId:'root-one',
  goalTitle:'Root one',subgoalTitle:'Child one'});
"""
        run = subprocess.run([node, "--input-type=module", "-"], input=prelude + body,
                             text=True, capture_output=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stderr)

    def test_only_verified_batches_create_one_notification_even_after_reload(self):
        self.run_js("""
const store=makeStore(), alerts=createNotificationActions(store,{storage});
store.set({phases:{'sub-one':phase('done')}});
alerts.update();
assert.equal(store.get().notificationItems.length,0,'historical completion seeds quietly');
const run='2026-09-09T11:00:00Z';
for(const status of ['building','checking','failed','fixing','needs_user','cancelled']) {
  store.set({phases:{'sub-one':phase(status,run)},slices:{'sub-one':{todos:[{id:'todo-one',done:true}]}}});
  alerts.update(); assert.equal(store.get().notificationItems.length,0,status+' is not verified completion');
}
store.set({phases:{'sub-one':phase('done',run)}}); alerts.update();
assert.equal(store.get().notificationItems.length,1);
assert.equal(store.get().notificationItems[0].todoCount,2);
assert.equal(store.get().notificationItems[0].read,false);
alerts.update(); alerts.update();
assert.equal(store.get().notificationItems.length,1,'polling does not repeat completion');
store.set({phases:{'sub-one':{...phase('done',run),todoIds:['todo-one']}}});alerts.update();
assert.equal(store.get().notificationItems.length,1,'a user edit does not create another batch');
const id=store.get().notificationItems[0].id;
alerts.readNotification(id);
const afterReload=makeStore(); afterReload.set({phases:{'sub-one':phase('done',run)}});
const fresh=createNotificationActions(afterReload,{storage});fresh.update();
assert.equal(afterReload.get().notificationItems.length,1);
assert.equal(afterReload.get().notificationItems[0].read,true,'read survives reload');
afterReload.set({phases:{'sub-one':phase('done','2026-09-09T12:00:00Z')}});fresh.update();
assert.equal(afterReload.get().notificationItems.length,2,'same todos can finish a new run');
""")

    def test_scope_offline_completion_and_nonselected_goal_links(self):
        self.run_js("""
const store=makeStore(), alerts=createNotificationActions(store,{storage});
store.set({phases:{'sub-one':phase('building')}});alerts.update();
const reopened=makeStore();
reopened.set({phases:{'sub-other':{...phase('done'),goalId:'root-two',goalTitle:'Root two',subgoalTitle:'Other child'}}});
const next=createNotificationActions(reopened,{storage});next.update();
assert.equal(reopened.get().notificationItems.length,1,'completion while closed is retained');
const item=reopened.get().notificationItems[0];
assert.equal(notificationHref(item),'/workspace?goal=root-two&subgoal=sub-other');
next.toggleNotifications(); assert.equal(reopened.get().notificationsOpen,true);
next.closeNotifications(); assert.equal(reopened.get().notificationsOpen,false);
reopened.set({notificationScope:{project:'/different',session:'chat-one'}});next.update();
assert.equal(reopened.get().notificationItems.length,0,'projects have independent inboxes');
reopened.set({notificationScope:{project:'/research',session:'chat-two'}});next.update();
assert.equal(reopened.get().notificationItems.length,0,'chats have independent inboxes');
reopened.set({notificationScope:{project:'/research',session:'chat-one'}});next.update();
assert.equal(reopened.get().notificationItems.length,1);
next.markNotificationsRead();assert.ok(reopened.get().notificationItems.every(x=>x.read));
""")

    def test_corrupt_storage_and_unavailable_storage_do_not_break_the_workspace(self):
        self.run_js("""
const bad={getItem:()=>'{broken',setItem:()=>{throw Error('quota');}};
const store=makeStore(), alerts=createNotificationActions(store,{storage:bad});
alerts.update();store.set({phases:{'sub-one':phase('done')}});alerts.update();
assert.equal(store.get().notificationItems.length,1);
alerts.update();assert.equal(store.get().notificationItems.length,1);
""")


class StandardPresentationBrowserTests(BrowserCase):
    def test_inline_answer_reaches_paused_build_runtime(self):
        goal, children = seed_design(self.chat)
        goals, important = self.goals()
        row = GM.by_id(goals, children[0])["todo_items"][0]
        row.update(status="asking", question="Which format should I use?")
        ui._save_goals(self.chat, goals, important, True)
        AGENT_EVENTS.record("chat", self.root, AGENT_EVENTS.new_event("chat.needs_human", "agent",
            {"question":row["question"],"rows":[row["id"]],"resume":"build"}, subgoal_id=children[0], todo_id=row["id"]))
        def resume(session, root, subgoal, row_id, text):
            current, flags = self.goals()
            found = next(r for r in GM.by_id(current, subgoal)["todo_items"] if r["id"] == row_id)
            found.update(status="building", question="")
            ui._save_goals(self.chat, current, flags, True)
            return {"ok":True}
        with mock.patch.object(CHAT_AGENT,"ask",return_value={"ok":True,"resolution":"resume","say":"Use CSV","needs":{}}), \
                mock.patch.object(RT.Runtime,"answer",side_effect=resume) as answer, \
                server_for(self.chat) as url, self.page_on(url) as (page, errors):
            field=page.get_by_label("Answer about " + row["text"], exact=True)
            self.expect(field).to_be_visible()
            field.fill("Use CSV and continue.")
            field.press("Enter")
            self.expect(field).to_have_count(0)
            self.assertEqual(1, answer.call_count)
            self.assertEqual(row["id"], answer.call_args.args[3])
            self.assertIn("Use CSV and continue.", answer.call_args.args[4])
            self.assertTrue(AGENT_EVENTS.read("chat",self.root,types=["human.answered"]))
            self.assertEqual([], errors)

    def test_actual_server_completion_updates_bell_once_and_read_survives_reload(self):
        goal, children=seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url) as (page, errors):
            page.wait_for_selector(".workspace-standard")
            page.wait_for_function("window.engelbart?.store.get().notificationScope?.session")
            for kind in ["build.started","verify.started","verify.passed"]:
                AGENT_EVENTS.record("chat", self.root, AGENT_EVENTS.new_event(kind,"system",{"rows":["ta","tb"]},subgoal_id=children[1]))
            page.evaluate("window.engelbart.actions.loadPanes()")
            bell=page.get_by_role("button",name="Notifications, 1 unread",exact=True)
            self.expect(bell).to_be_visible()
            page.evaluate("window.engelbart.actions.loadPanes()")
            bell.click()
            item=page.locator(".notification-item")
            self.expect(item).to_have_count(1)
            self.expect(item).to_contain_text("2 todos completed")
            item.click()
            self.assertEqual(children[1],page.evaluate("window.engelbart.store.get().activeId"))
            self.assertTrue(page.evaluate("window.engelbart.store.get().notificationItems[0].read"))
            page.reload(wait_until="domcontentloaded")
            page.get_by_role("button",name="Notifications",exact=True).click()
            self.expect(page.locator(".notification-item")).to_have_count(1)
            self.expect(page.locator(".notification-item.is-unread")).to_have_count(0)
            self.assertEqual([],errors)

    def test_chat_renders_markdown_as_safe_dom_in_the_real_component(self):
        seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url + "/workspace") as (page, _errors):
            page.wait_for_selector(".workspace-standard")
            result = page.evaluate("""async () => {
              const {renderBrainstorm}=await import('/goal/components/brainstorm.js');
              const text='`/bart` opens **this chat** with *emphasis*, ***both*** and [a link](https://example.com/a_(b)).\\n\\n- One\\n  - Nested\\n- Two\\n\\n```js\\nconst value = "<b>literal</b>";\\n```\\n\\n<img src=x onerror=alert(1)> [unsafe](javascript:alert(1))';
              const actions=new Proxy({}, {get:()=>()=>{}});
              const state={activeId:'child',slices:{child:{draft:'',thinking:false,clearing:false,
                todos:[],chat:[{id:'m1',who:'bart',kind:'text',text}]}}};
              const host=document.createElement('div');host.id='markdown-test';host.className='workspace-standard';
              host.append(renderBrainstorm(state,actions,true));document.body.append(host);
              const bubble=host.querySelector('.bubble');
              return {strong:[...bubble.querySelectorAll('strong')].map(n=>n.textContent),
                emphasis:[...bubble.querySelectorAll('em')].map(n=>n.textContent),
                code:[...bubble.querySelectorAll('code')].map(n=>n.textContent),
                nested:!!bubble.querySelector('ul ul'),
                links:[...bubble.querySelectorAll('a')].map(n=>({href:n.href,rel:n.rel})),
                dangerous:bubble.querySelectorAll('img,script,iframe').length,
                text:bubble.textContent,
                labelStyle:getComputedStyle(host.querySelector('.msg-who')).textTransform};
            }""")
            self.assertIn("this chat", result["strong"])
            self.assertIn("both", result["strong"])
            self.assertIn("emphasis", result["emphasis"])
            self.assertEqual("/bart", result["code"][0])
            self.assertIn('<b>literal</b>', result["code"][1])
            self.assertTrue(result["nested"])
            self.assertEqual(1, len(result["links"]))
            self.assertEqual("https://example.com/a_(b)", result["links"][0]["href"])
            self.assertIn("noopener", result["links"][0]["rel"])
            self.assertEqual(0, result["dangerous"])
            self.assertIn("<img src=x", result["text"])
            self.assertEqual("none", result["labelStyle"])
            safe = page.evaluate("""async()=>{
              const {safeMarkdownHref}=await import('/goal/markdown.js');
              return ['javascript:alert(1)','java\\nscript:alert(1)','data:text/html,x','//evil.test','/\\\\evil.test',
                'https://example.com','#section','/workspace?goal=g'].map(safeMarkdownHref);
            }""")
            self.assertEqual([None] * 5, safe[:5])
            self.assertTrue(all(safe[5:]))

    def test_centered_dataset_controls_keep_file_folder_and_native_picker_handlers(self):
        seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url + "/workspace") as (page, _errors):
            page.wait_for_selector(".workspace-standard")
            page.evaluate("""async()=>{
              const {renderResourcePane}=await import('/goal/components/resources.js');
              window.datasetCalls=[];
              const state={tab:'dataset',project:{resources:[]}};
              const actions={uploadDataset:value=>window.datasetCalls.push(value instanceof File ? value.name : value.map(x=>x.path)),
                chooseLocalDataset:()=>window.datasetCalls.push('local-picker'),datasetUploadError:()=>{}};
              const host=document.createElement('div');host.id='dataset-test';host.className='workspace-standard';
              host.append(renderResourcePane(state,actions));document.querySelector('#app').style.display='none';document.body.append(host);
            }""")
            self.expect(page.get_by_role("heading", name="Add your dataset")).to_be_visible()
            page.get_by_label("Upload dataset", exact=True).set_input_files(
                {"name":"example.csv", "mimeType":"text/csv", "buffer":b"x,y\n1,2\n"})
            page.get_by_role("button", name="Choose local folder").click()
            self.assertEqual(["example.csv", "local-picker"], page.evaluate("window.datasetCalls"))
            self.assertTrue(page.get_by_label("Choose dataset folder").get_attribute("webkitdirectory") is not None)
            position = page.locator(".dataset-empty-card").bounding_box()
            section = page.locator(".resource-dataset").bounding_box()
            self.assertAlmostEqual(position["x"] + position["width"] / 2,
                                   section["x"] + section["width"] / 2, delta=2)

    def test_projects_are_readable_on_wide_and_small_screens(self):
        seed_design(self.chat)
        with server_for(self.chat) as url, self.page_on(url + "/workspace") as (page, _errors):
            page.wait_for_selector(".workspace-standard")
            page.evaluate("""async()=>{
              const {renderProjects}=await import('/goal/components/home.js');
              const rows=Array.from({length:10},(_,i)=>({cwd:'/projects/'+i,name:'Project '+i,objective:'Research purpose',goals:4,chats:2}));
              const host=document.createElement('div');host.className='workspace-standard';
              host.append(renderProjects({projects:rows,projectsHere:'/projects/0'},{openProject:()=>{}}));
              document.querySelector('#app').style.display='none';document.body.append(host);
            }""")
            page.set_viewport_size({"width":2200,"height":1000})
            columns = page.locator(".home-grid").evaluate("el=>getComputedStyle(el).gridTemplateColumns.split(' ').length")
            self.assertEqual(3, columns)
            page.set_viewport_size({"width":580,"height":850})
            columns = page.locator(".home-grid").evaluate("el=>getComputedStyle(el).gridTemplateColumns.split(' ').length")
            self.assertEqual(1, columns)


if __name__ == "__main__":
    unittest.main()
