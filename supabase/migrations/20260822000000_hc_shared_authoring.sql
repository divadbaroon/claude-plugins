-- Creating, moving and re-listing goals from the shared workspace.
--
-- Until now a shared tree could only change the words on a goal that was
-- already there. Adding one, dragging it somewhere else, or editing its
-- TODO rows all reported success and wrote nothing -- which is worse than
-- refusing, because the reader watches their work appear and then vanish
-- on the next poll.
--
-- All three are the caller's own rows only; row security decides that, as
-- everywhere else. What is added here is the concurrency check, so moving
-- or re-listing a goal somebody else has since changed is refused with the
-- same "it moved under you" that editing already gives.

-- A goal authored in the shared workspace belongs to the project, not to
-- any of the author's chats -- there is no vault behind this window, and
-- inventing a chat in someone's own store to hold it would be worse than
-- saying where it lives. session_id says so plainly.
create or replace function public.hc_create_goal(
  p_project_id uuid,
  p_title text,
  p_parent_id uuid default null)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  made public.hc_goals;
  local text;
begin
  if uid is null then
    raise exception 'hc_create_goal: no authenticated user';
  end if;
  if not public.hc_can_write(p_project_id) then
    return jsonb_build_object('ok', false, 'error',
      'you are not a contributor to this project');
  end if;
  -- A parent must be in the same project. It need not be the caller's --
  -- hanging your goal under someone else's is a fair thing to want, and
  -- changes nothing about who may edit either one.
  if p_parent_id is not null and not exists (
        select 1 from public.hc_goals g
         where g.id = p_parent_id and g.project_id = p_project_id) then
    return jsonb_build_object('ok', false, 'error',
      'that parent is not in this project');
  end if;
  local := 'sh' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 10);
  insert into public.hc_goals
    (id, user_id, project_id, session_id, local_id, parent_id, title,
     status, priority, origin, updated_at)
  values (gen_random_uuid(), uid, p_project_id, 'shared', local, p_parent_id,
          coalesce(nullif(btrim(p_title), ''), 'Untitled'),
          'active', 'normal', 'shared', now())
  returning * into made;
  return jsonb_build_object('ok', true, 'id', made.id,
    'local_id', made.local_id, 'session_id', made.session_id,
    'updated_at', made.updated_at);
end $$;

-- Moving a goal. Separate from hc_update_goal because a move is not a
-- field edit: it can make a cycle, and that has to be refused rather than
-- written and discovered later by whatever walks the tree.
create or replace function public.hc_move_goal(
  p_goal_id uuid,
  p_expect timestamptz,
  p_parent_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  current public.hc_goals;
  walker uuid;
  hops integer := 0;
begin
  if uid is null then
    raise exception 'hc_move_goal: no authenticated user';
  end if;
  select * into current from public.hc_goals g where g.id = p_goal_id;
  if current.id is null then
    return jsonb_build_object('ok', false, 'error', 'no such goal');
  end if;
  if current.user_id <> uid then
    return jsonb_build_object('ok', false, 'error',
      'that goal belongs to someone else');
  end if;
  if p_expect is not null and current.updated_at is distinct from p_expect then
    return jsonb_build_object('ok', false, 'conflict', true,
      'error', 'this goal changed while you were editing');
  end if;
  if p_parent_id is not null then
    if not exists (select 1 from public.hc_goals g
                    where g.id = p_parent_id
                      and g.project_id = current.project_id) then
      return jsonb_build_object('ok', false, 'error',
        'that parent is not in this project');
    end if;
    -- Walk up from the intended parent: meeting the goal itself means the
    -- move would close a loop and orphan the branch from the roots.
    walker := p_parent_id;
    while walker is not null and hops < 200 loop
      if walker = p_goal_id then
        return jsonb_build_object('ok', false, 'error',
          'that would put the goal inside itself');
      end if;
      select parent_id into walker from public.hc_goals where id = walker;
      hops := hops + 1;
    end loop;
  end if;
  update public.hc_goals set parent_id = p_parent_id, updated_at = now()
   where id = p_goal_id;
  return jsonb_build_object('ok', true);
end $$;

-- The TODO rail of one goal, as a set. Rows have no tombstone of their own
-- -- a row the browser stops sending is deleted -- so the whole list is
-- replaced rather than merged, which is also how the local rail saves.
create or replace function public.hc_replace_todos(
  p_goal_id uuid,
  p_expect timestamptz,
  p_rows jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  current public.hc_goals;
  kept uuid[];
begin
  if uid is null then
    raise exception 'hc_replace_todos: no authenticated user';
  end if;
  select * into current from public.hc_goals g where g.id = p_goal_id;
  if current.id is null then
    return jsonb_build_object('ok', false, 'error', 'no such goal');
  end if;
  if current.user_id <> uid then
    return jsonb_build_object('ok', false, 'error',
      'that goal belongs to someone else');
  end if;
  if p_expect is not null and current.updated_at is distinct from p_expect then
    return jsonb_build_object('ok', false, 'conflict', true,
      'error', 'this goal changed while you were editing');
  end if;

  insert into public.hc_todos
    (id, user_id, project_id, goal_id, local_id, position, depth, text,
     status, question, updated_at)
  select
    coalesce((select t.id from public.hc_todos t
               where t.goal_id = p_goal_id
                 and t.local_id = r ->> 'local_id' limit 1),
             gen_random_uuid()),
    uid, current.project_id, p_goal_id,
    coalesce(nullif(r ->> 'local_id', ''), 'r' || (ordinality::text)),
    (ordinality - 1)::int,
    least(greatest(coalesce((r ->> 'depth')::int, 0), 0), 8),
    coalesce(r ->> 'text', ''),
    case when r ->> 'status' in ('', 'queued', 'building', 'asking',
                                 'done', 'failed')
         then r ->> 'status' else '' end,
    coalesce(r ->> 'question', ''), now()
  from jsonb_array_elements(coalesce(p_rows, '[]'::jsonb))
       with ordinality as e(r, ordinality)
  on conflict (id) do update set
    local_id = excluded.local_id, position = excluded.position,
    depth = excluded.depth, text = excluded.text,
    status = excluded.status, question = excluded.question,
    updated_at = now();

  select coalesce(array_agg(t.id), '{}') into kept
    from public.hc_todos t
   where t.goal_id = p_goal_id
     and t.local_id in (select coalesce(nullif(r ->> 'local_id', ''), '\x00')
                        from jsonb_array_elements(
                               coalesce(p_rows, '[]'::jsonb)) r);
  delete from public.hc_todos t
   where t.goal_id = p_goal_id and t.user_id = uid
     and not (t.id = any(kept));

  update public.hc_goals set updated_at = now() where id = p_goal_id;
  return jsonb_build_object('ok', true,
    'rows', (select count(*) from public.hc_todos where goal_id = p_goal_id));
end $$;

revoke all on function public.hc_create_goal(uuid, text, uuid) from public;
revoke all on function public.hc_move_goal(uuid, timestamptz, uuid) from public;
revoke all on function public.hc_replace_todos(uuid, timestamptz, jsonb) from public;
grant execute on function public.hc_create_goal(uuid, text, uuid) to authenticated;
grant execute on function public.hc_move_goal(uuid, timestamptz, uuid) to authenticated;
grant execute on function public.hc_replace_todos(uuid, timestamptz, jsonb) to authenticated;
