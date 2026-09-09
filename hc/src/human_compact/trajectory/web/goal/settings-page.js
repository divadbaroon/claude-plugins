/* Both interfaces use exactly the same account, billing and model controls. */
import { createStore, initialState } from "./store.js";
import { createActions } from "./actions.js";
import { services } from "./services.js";
import { h, mount } from "./dom.js";
import { renderSettingsContent } from "./components/header.js";

const mode = new URLSearchParams(location.search).get("interface") === "legacy" ? "legacy" : "goal";
const store = createStore({...initialState(), interfaceMode:mode});
const actions = createActions(store, services);
const host = document.getElementById("app");
host.style.cssText = "max-width:440px;margin:0 auto;padding:20px;overflow:auto;height:100vh";
document.title = "Settings · Engelbart";
function draw(state) {
  mount(host, h("main", {"aria-label":"Workspace settings"}, renderSettingsContent(state, actions)));
}
store.subscribe(draw);
draw(store.get());
actions.loadAccount();
actions.loadReader();
