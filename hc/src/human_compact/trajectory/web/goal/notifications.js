/* One persisted inbox per project and chat, driven only by server lifecycle
   verification. A TODO's checkbox and a stopped process are not completion. */
const PREFIX = "engelbart:build-notifications:v1:";
const MAX_ITEMS = 100;

export function notificationScopeKey(scope) {
  if (!scope || typeof scope.project !== "string" || typeof scope.session !== "string"
      || !scope.project || !scope.session) return null;
  return PREFIX + JSON.stringify([scope.project, scope.session]);
}

export function notificationHref(item) {
  const params = new URLSearchParams({goal:item.goalId, subgoal:item.subgoalId});
  return `/workspace?${params}`;
}

function safeStorage() { try { return globalThis.localStorage; } catch (_) { return null; } }

export function createNotificationActions(store, options = {}) {
  const storage = options.storage === undefined ? safeStorage() : options.storage;
  let currentKey = null, record = null;
  const read = key => {
    try {
      const value = JSON.parse(storage?.getItem(key) || "null");
      if (value?.version === 1 && Array.isArray(value.seen) && Array.isArray(value.items)) {
        return {version:1, initialized:Boolean(value.initialized), seen:value.seen.filter(x=>typeof x === "string"),
          items:value.items.filter(item=>item && typeof item.id === "string" && typeof item.subgoalId === "string"
            && typeof item.goalId === "string").slice(0, MAX_ITEMS)};
      }
    } catch (_) { /* Local preferences can be unavailable or from an older build. */ }
    return {version:1, initialized:false, seen:[], items:[]};
  };
  const save = () => { try { if (currentKey) storage?.setItem(currentKey, JSON.stringify(record)); } catch (_) {} };
  const publish = patch => store.set({notificationItems:record?.items || [], ...patch});

  function update(snapshot = store.get()) {
    const key = notificationScopeKey(snapshot.notificationScope);
    if (!key) return;
    const switched = key !== currentKey;
    if (switched) { currentKey = key; record = read(key); }
    if (!snapshot.phases || typeof snapshot.phases !== "object") {
      if (switched) publish({notificationsOpen:false});
      return;
    }
    // Pick up reads from another tab before adding a new completion.
    const persisted = read(key);
    const seen = new Set([...record.seen, ...persisted.seen]);
    const readIds = new Set(persisted.items.filter(item=>item.read).map(item=>item.id));
    const existing = new Map([...persisted.items, ...record.items].map(item=>[item.id, {...item, read:item.read || readIds.has(item.id)}]));
    let changed = switched || !record.initialized;
    for (const [subgoalId, phase] of Object.entries(snapshot.phases)) {
      if (phase?.status !== "done" || !phase.at || !Array.isArray(phase.todoIds) || !phase.todoIds.length) continue;
      const id = JSON.stringify([subgoalId, phase.startedAt || phase.at]);
      if (seen.has(id)) continue;
      seen.add(id); changed = true;
      // Do not flood a first-time visitor with already completed historical work.
      if (!record.initialized) continue;
      const subgoal = snapshot.subgoals?.find(row=>row.id === subgoalId);
      const goalId = phase.goalId || (subgoal ? snapshot.goal?.id : "");
      if (!goalId) continue;
      existing.set(id, {id, subgoalId, goalId,
        goalTitle:phase.goalTitle || snapshot.goal?.title || "Goal",
        subgoalTitle:phase.subgoalTitle || subgoal?.title || "Subgoal",
        todoCount:new Set(phase.todoIds).size, at:phase.at, read:false});
    }
    const items = [...existing.values()].sort((a,b)=>String(b.at).localeCompare(String(a.at))).slice(0, MAX_ITEMS);
    if (JSON.stringify(items) !== JSON.stringify(record.items)) changed = true;
    record = {version:1, initialized:true, seen:[...seen], items};
    save();
    if (changed) publish(switched ? {notificationsOpen:false} : {});
  }

  function readNotification(id) {
    if (!record) return null;
    const item = record.items.find(row=>row.id === id);
    if (!item) return null;
    record = {...record, items:record.items.map(row=>row.id === id ? {...row, read:true} : row)};
    save(); publish(); return item;
  }

  function markNotificationsRead() {
    if (!record || record.items.every(item=>item.read)) return;
    record = {...record, items:record.items.map(item=>({...item, read:true}))};
    save(); publish();
  }

  function closeNotifications() {
    if (store.get().notificationsOpen) store.set({notificationsOpen:false});
  }

  return {update, readNotification, markNotificationsRead, closeNotifications,
    toggleNotifications() { store.set({notificationsOpen:!store.get().notificationsOpen, accountOpen:false}); }};
}
