-- Deleting by tombstone, not by absence.
--
-- The workspace already keeps tombstones and has all along: deleting a goal
-- does not remove it from goals.json, it sets status='abandoned' and keeps
-- it for good ("a tombstone is sticky: nothing restores a deleted goal").
-- Five such goals are in this project right now. So a deleted goal reaches
-- Postgres as a STATUS, and the prune below was never what removed it.
--
-- What the prune actually did was answer the other question -- "this row is
-- not in the payload" -- with "so delete it". That is right when one
-- snapshot is the whole truth and wrong the moment anything else can write.
-- A goal authored in the shared workspace is absent from its author's local
-- snapshot for a plain and innocent reason: the vault has never heard of
-- it. The next personal Save would delete it. No conflict, no warning.
--
-- So: goals are never pruned. Their children still are -- a TODO row the
-- browser stops sending IS deleted, there is no tombstone for it -- but
-- only among the goals the payload actually carries. Rows hanging off a
-- goal this payload says nothing about are none of its business.

create or replace function public.hc_sync_project(payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  pid uuid := (payload ->> 'project_id')::uuid;
  owns boolean;
  touched uuid[];
begin
  if uid is null then
    raise exception 'hc_sync_project: no authenticated user';
  end if;
  if pid is null then
    raise exception 'hc_sync_project: payload has no project_id';
  end if;
  if (payload ->> 'user_id') is distinct from uid::text then
    raise exception 'hc_sync_project: payload is not owned by the caller';
  end if;

  owns := exists (select 1 from public.hc_projects p
                   where p.id = pid and p.user_id = uid);

  if owns or not exists (select 1 from public.hc_projects p where p.id = pid) then
    insert into public.hc_projects
      (id, user_id, cwd, name, objective, description, generated_at, updated_at)
    select (r ->> 'id')::uuid, uid, coalesce(r ->> 'cwd', ''),
           coalesce(r ->> 'name', ''), coalesce(r ->> 'objective', ''),
           coalesce(r ->> 'description', ''),
           (r ->> 'generated_at')::timestamptz, now()
    from jsonb_array_elements(payload -> 'projects') r
    on conflict (id) do update set
      cwd = excluded.cwd, name = excluded.name,
      objective = excluded.objective, description = excluded.description,
      generated_at = excluded.generated_at, updated_at = now();
    owns := true;
  elsif not public.hc_can_write(pid) then
    raise exception 'hc_sync_project: you are not a contributor to this project';
  end if;

  insert into public.hc_chats
    (id, user_id, project_id, session_id, created_at, updated_at,
     prompt_count, goal_count)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'session_id',
         (r ->> 'created_at')::timestamptz, (r ->> 'updated_at')::timestamptz,
         coalesce((r ->> 'prompt_count')::int, 0),
         coalesce((r ->> 'goal_count')::int, 0)
  from jsonb_array_elements(payload -> 'chats') r
  on conflict (id) do update set
    session_id = excluded.session_id, created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    prompt_count = excluded.prompt_count, goal_count = excluded.goal_count;

  insert into public.hc_goals
    (id, user_id, project_id, session_id, local_id, parent_id, title, status,
     priority, origin, description, notes, prompt, evidence_ids, important,
     updated_at)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'session_id', r ->> 'local_id',
         null, coalesce(r ->> 'title', ''),
         coalesce(r ->> 'status', 'active'),
         coalesce(r ->> 'priority', 'normal'), coalesce(r ->> 'origin', ''),
         coalesce(r ->> 'description', ''), coalesce(r ->> 'notes', ''),
         coalesce(r ->> 'prompt', ''),
         coalesce((select array_agg(value #>> '{}')
                   from jsonb_array_elements(r -> 'evidence_ids')), '{}'),
         coalesce(r -> 'important', '[]'::jsonb),
         (r ->> 'updated_at')::timestamptz
  from jsonb_array_elements(payload -> 'goals') r
  on conflict (id) do update set
    session_id = excluded.session_id, local_id = excluded.local_id,
    title = excluded.title, status = excluded.status,
    priority = excluded.priority, origin = excluded.origin,
    description = excluded.description, notes = excluded.notes,
    prompt = excluded.prompt, evidence_ids = excluded.evidence_ids,
    important = excluded.important, updated_at = excluded.updated_at;

  update public.hc_goals g set parent_id = (r ->> 'parent_id')::uuid
  from jsonb_array_elements(payload -> 'goals') r
  where g.id = (r ->> 'id')::uuid
    and g.parent_id is distinct from (r ->> 'parent_id')::uuid;

  -- The goals this payload speaks for. Everything below is scoped to them.
  select coalesce(array_agg((r ->> 'id')::uuid), '{}')
    into touched
    from jsonb_array_elements(payload -> 'goals') r;

  insert into public.hc_todos
    (id, user_id, project_id, goal_id, local_id, position, depth, text,
     status, question)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         r ->> 'local_id', coalesce((r ->> 'position')::int, 0),
         coalesce((r ->> 'depth')::int, 0), coalesce(r ->> 'text', ''),
         coalesce(r ->> 'status', ''), coalesce(r ->> 'question', '')
  from jsonb_array_elements(payload -> 'todos') r
  on conflict (id) do update set
    local_id = excluded.local_id, position = excluded.position,
    depth = excluded.depth, text = excluded.text,
    status = excluded.status, question = excluded.question;

  insert into public.hc_goal_sources
    (id, user_id, project_id, goal_id, local_id, type, label, position)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         coalesce(r ->> 'local_id', ''), coalesce(r ->> 'type', ''),
         coalesce(r ->> 'label', ''), coalesce((r ->> 'position')::int, 0)
  from jsonb_array_elements(payload -> 'goal_sources') r
  on conflict (id) do update set
    local_id = excluded.local_id, type = excluded.type,
    label = excluded.label, position = excluded.position;

  if owns then
    insert into public.hc_project_sources
      (id, user_id, project_id, local_id, type, label, position)
    select (r ->> 'id')::uuid, uid, pid, coalesce(r ->> 'local_id', ''),
           coalesce(r ->> 'type', ''), coalesce(r ->> 'label', ''),
           coalesce((r ->> 'position')::int, 0)
    from jsonb_array_elements(payload -> 'project_sources') r
    on conflict (id) do update set
      local_id = excluded.local_id, type = excluded.type,
      label = excluded.label, position = excluded.position;
  end if;

  insert into public.hc_related_prompts
    (id, user_id, project_id, goal_id, prompt_id, text, session_id, auto,
     created_at, position)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         r ->> 'prompt_id', coalesce(r ->> 'text', ''),
         coalesce(r ->> 'session_id', ''),
         coalesce((r ->> 'auto')::boolean, false),
         (r ->> 'created_at')::timestamptz,
         coalesce((r ->> 'position')::int, 0)
  from jsonb_array_elements(payload -> 'related_prompts') r
  on conflict (id) do update set
    prompt_id = excluded.prompt_id, text = excluded.text,
    session_id = excluded.session_id, auto = excluded.auto,
    created_at = excluded.created_at, position = excluded.position;

  -- GOALS ARE NEVER PRUNED. A deleted one arrives as status='abandoned';
  -- an absent one was written somewhere this payload cannot see.

  -- Children are, because they have no tombstone of their own -- but only
  -- under the goals this payload carried, and only this caller's own.
  delete from public.hc_todos t
   where t.project_id = pid and t.user_id = uid
     and t.goal_id = any(touched)
     and t.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'todos') r);
  delete from public.hc_goal_sources s
   where s.project_id = pid and s.user_id = uid
     and s.goal_id = any(touched)
     and s.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'goal_sources') r);
  delete from public.hc_related_prompts p
   where p.project_id = pid and p.user_id = uid
     and p.goal_id = any(touched)
     and p.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'related_prompts') r);
  if owns then
    delete from public.hc_project_sources s
     where s.project_id = pid and s.user_id = uid
       and s.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                        from jsonb_array_elements(payload -> 'project_sources') r);
  end if;

  return jsonb_build_object(
    'ok', true, 'project_id', pid, 'owner', owns,
    'goals', (select count(*) from public.hc_goals where project_id = pid),
    'todos', (select count(*) from public.hc_todos where project_id = pid),
    'mine', jsonb_build_object(
      'goals', (select count(*) from public.hc_goals
                 where project_id = pid and user_id = uid),
      'tombstoned', (select count(*) from public.hc_goals
                      where project_id = pid and user_id = uid
                        and status = 'abandoned')));
end $$;

revoke all on function public.hc_sync_project(jsonb) from public;
grant execute on function public.hc_sync_project(jsonb) to authenticated;
