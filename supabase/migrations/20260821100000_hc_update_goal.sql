-- Editing one goal, with a check that nobody moved it first.
--
-- The reader opened the goal some seconds ago and has been typing. In that
-- time the row may have changed -- by their own other window, or later by a
-- collaborator. Writing blindly means whoever saves last wins and the other
-- edit is gone with nothing said.
--
-- So the write carries the updated_at it read. If the row still shows that,
-- the edit lands. If it does not, nothing is written and the caller is told
-- it was overtaken -- a question the reader can answer, instead of a loss
-- they never learn about.
--
-- Who may write is not decided here: this is SECURITY INVOKER, so the row
-- policies do it. A member editing someone else's goal simply matches no
-- row, and gets the same honest "not yours" as if they had tried directly.

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
      when p_fields ->> 'status' in ('active', 'in_progress', 'completed',
                                     'abandoned')
      then p_fields ->> 'status' else g.status end,
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
