-- Two people pushing into one project without deleting each other.
--
-- hc_sync_project prunes what its payload does not contain -- that is what
-- makes a goal deleted locally disappear from the remote. With one writer
-- that is right. With two it is a wipe: a collaborator's snapshot is built
-- from their own vault, so it contains none of the owner's goals, and the
-- first push would delete every one of them.
--
-- The fix is to prune only the pusher's own rows. Every row already carries
-- user_id, so that is the boundary, and it needs no bookkeeping: what I did
-- not send, of mine, is gone; what is yours is untouched.
--
-- This also settles what the two roles mean:
--   reader  -- sees the project, pushes nothing
--   editor  -- sees the project and contributes THEIR OWN goals
-- Neither can change the other's rows. That is not a policy decision made
-- here; row security already forbids it, because every write is checked
-- against user_id. Co-editing one goal remains impossible on purpose --
-- it needs an answer to what happens when two people change it at once.

-- Who may push into a project at all: its owner, or an editor on the roll.
create or replace function public.hc_can_write(pid uuid)
returns boolean
language sql
security definer
stable
set search_path = public
as $$
  select exists (select 1 from public.hc_projects p
                  where p.id = pid and p.user_id = (select auth.uid()))
      or exists (select 1 from public.hc_project_members m
                  where m.project_id = pid
                    and m.user_id = (select auth.uid())
                    and m.role = 'editor')
$$;

revoke all on function public.hc_can_write(uuid) from public;
grant execute on function public.hc_can_write(uuid) to authenticated;

-- A row may only be written into a project you own or contribute to. Without
-- this, anyone knowing a project's id could insert rows into it under their
-- own user_id -- invisible to its owner, but there.
do $$
declare t text;
begin
  foreach t in array array['hc_project_sources', 'hc_chats', 'hc_goals',
                           'hc_todos', 'hc_goal_sources', 'hc_related_prompts']
  loop
    execute format('drop policy if exists %I on public.%I', t || '_owner', t);
    execute format(
      'create policy %I on public.%I for all to authenticated '
      'using (user_id = (select auth.uid())) '
      'with check (user_id = (select auth.uid()) '
      '            and public.hc_can_write(project_id))',
      t || '_owner', t);
  end loop;
end $$;

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
  pruned jsonb;
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

  -- A project nobody has claimed is claimed by whoever pushes it first.
  -- After that only its owner writes the project row: one row, one set of
  -- details, and no silent tug of war over the objective.
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

  -- THE PRUNE, and every clause of it says `user_id = uid`. What I did not
  -- send, of mine, is gone. What is yours I never look at.
  delete from public.hc_todos t
   where t.project_id = pid and t.user_id = uid
     and t.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'todos') r);
  delete from public.hc_goal_sources s
   where s.project_id = pid and s.user_id = uid
     and s.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'goal_sources') r);
  delete from public.hc_related_prompts p
   where p.project_id = pid and p.user_id = uid
     and p.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'related_prompts') r);
  delete from public.hc_goals g
   where g.project_id = pid and g.user_id = uid
     and g.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'goals') r);
  delete from public.hc_chats c
   where c.project_id = pid and c.user_id = uid
     and c.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                      from jsonb_array_elements(payload -> 'chats') r);
  if owns then
    delete from public.hc_project_sources s
     where s.project_id = pid and s.user_id = uid
       and s.id not in (select coalesce((r ->> 'id')::uuid, gen_random_uuid())
                        from jsonb_array_elements(payload -> 'project_sources') r);
  end if;

  pruned := jsonb_build_object(
    'mine_goals', (select count(*) from public.hc_goals
                    where project_id = pid and user_id = uid),
    'mine_todos', (select count(*) from public.hc_todos
                    where project_id = pid and user_id = uid));
  return jsonb_build_object(
    'ok', true, 'project_id', pid, 'owner', owns,
    'goals', (select count(*) from public.hc_goals where project_id = pid),
    'todos', (select count(*) from public.hc_todos where project_id = pid),
    'mine', pruned);
end $$;

revoke all on function public.hc_sync_project(jsonb) from public;
grant execute on function public.hc_sync_project(jsonb) to authenticated;
