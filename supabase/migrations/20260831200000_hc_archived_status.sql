-- "abandoned" becomes "archived".
--
-- The status a delete writes has always meant "kept, out of the tree, findable
-- again" -- a record put away, not one given up on. The word said the opposite,
-- and now that there is an Archive view to find these in, the word is the one
-- the view uses.
--
-- Nothing about the meaning changes: goals are still never pruned by
-- hc_sync_project, and a goal carrying this status is still every bit as
-- present as an active one. Only the spelling moves.
--
-- Order matters below. The rows are rewritten BEFORE the check constraint is
-- swapped, because a constraint that no longer admits 'abandoned' cannot be
-- added while rows still say it.

-- 1. Every goal already put away, in the new spelling.
update public.hc_goals set status = 'archived' where status = 'abandoned';

-- 2. The vocabulary itself. Dropped and re-added rather than altered: a check
--    constraint has no ALTER, and IF EXISTS makes this safe to re-run.
alter table public.hc_goals drop constraint if exists hc_goals_status_known;
alter table public.hc_goals add constraint hc_goals_status_known check (
  status in ('active', 'in_progress', 'completed', 'archived'));

-- 3. hc_sync_project mentions the old word once, in the count of tombstones it
--    reports back. The function is a hundred and forty lines and belongs to an
--    earlier migration; copying it here to change one string would leave two
--    copies to drift apart. Instead the definition is read back from the
--    catalog, the word is replaced in it, and the result is re-declared --
--    whatever the latest definition happens to be, which is the point.
do $mig$
declare
  src text;
begin
  select pg_get_functiondef(p.oid) into src
    from pg_proc p
    join pg_namespace n on n.oid = p.pronamespace
   where n.nspname = 'public'
     and p.proname = 'hc_sync_project'
   limit 1;
  if src is null then
    raise notice 'hc_sync_project not present; nothing to rewrite';
  else
    execute replace(src, '''abandoned''', '''archived''');
  end if;
end $mig$;

-- 4. The single-goal write. Copied in full because this IS its definition
--    from here on. A client cached before the rename still posts the old
--    word, so it is accepted and stored as the new one rather than silently
--    ignored -- an ignored status would leave a deleted goal looking alive.
create or replace function public.hc_update_goal(
  p_goal_id uuid,
  p_expect timestamptz,
  p_fields jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  current public.hc_goals;
  fresh public.hc_goals;
  want text := case when p_fields ->> 'status' = 'abandoned'
                    then 'archived' else p_fields ->> 'status' end;
begin
  if uid is null then
    raise exception 'hc_update_goal: no authenticated user';
  end if;

  select * into current from public.hc_goals g where g.id = p_goal_id;
  if current.id is null then
    -- Either it does not exist or the policies hide it; from here those
    -- are the same fact and neither is worth telling apart.
    return jsonb_build_object('ok', false, 'error', 'no such goal');
  end if;
  if current.user_id <> uid then
    return jsonb_build_object('ok', false, 'error',
      'that goal belongs to someone else');
  end if;
  if p_expect is not null and current.updated_at is distinct from p_expect then
    return jsonb_build_object('ok', false, 'conflict', true,
      'error', 'this goal changed while you were editing',
      'updated_at', current.updated_at,
      'title', current.title, 'notes', current.notes,
      'status', current.status, 'prompt', current.prompt,
      'description', current.description);
  end if;

  update public.hc_goals g set
    title = coalesce(p_fields ->> 'title', g.title),
    notes = coalesce(p_fields ->> 'notes', g.notes),
    description = coalesce(p_fields ->> 'description', g.description),
    prompt = coalesce(p_fields ->> 'prompt', g.prompt),
    status = case
      when want in ('active', 'in_progress', 'completed', 'archived')
      then want else g.status end,
    priority = case
      when p_fields ->> 'priority' in ('urgent', 'high', 'normal')
      then p_fields ->> 'priority' else g.priority end,
    updated_at = now()
   where g.id = p_goal_id
   returning * into fresh;

  if fresh.id is null then
    return jsonb_build_object('ok', false, 'error',
      'the write was refused: that goal is not yours to change');
  end if;
  return jsonb_build_object('ok', true, 'updated_at', fresh.updated_at,
    'session_id', fresh.session_id, 'local_id', fresh.local_id,
    'title', fresh.title, 'status', fresh.status);
end $$;

revoke all on function public.hc_update_goal(uuid, timestamptz, jsonb) from public;
grant execute on function public.hc_update_goal(uuid, timestamptz, jsonb) to authenticated;
