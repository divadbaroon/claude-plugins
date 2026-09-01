-- Deleting a goal for good.
--
-- Every other delete in this system keeps the record: the goal is marked
-- archived and stays, because a snapshot that lacks a goal is not evidence
-- that anybody deleted it (see hc_prune_by_tombstone). That is why goals are
-- never pruned by the sync, and why nothing until now could actually remove
-- one from Postgres.
--
-- The Archive view is where a person says the other thing -- erase this --
-- and this is the only path that does it. It is asked for by name, with one
-- goal's id, after a confirmation: nothing infers it from an absence.
--
-- The subtree goes too. A goal's children are goals, and leaving them behind
-- with parent_id set to null (which is what the foreign key would do on its
-- own) would turn "delete this goal" into "scatter its subgoals across the
-- top of the project". Rows, sources and related prompts hang off each of
-- those goals with ON DELETE CASCADE and go with them.
--
-- SECURITY INVOKER, and the caller's own rows only: the row policies decide
-- what is visible, and the explicit user_id check decides what is erasable.
-- A member cannot delete a teammate's goal out of a shared project.

create or replace function public.hc_delete_goal(p_goal_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  current public.hc_goals;
  doomed uuid[];
  removed integer;
begin
  if uid is null then
    raise exception 'hc_delete_goal: no authenticated user';
  end if;

  select * into current from public.hc_goals g where g.id = p_goal_id;
  if current.id is null then
    -- Nothing there. Which is the right end state for a caller asking for
    -- this goal to be gone, and is the ordinary case for a goal made and
    -- deleted between two syncs -- it was never up here to begin with.
    return jsonb_build_object('ok', true, 'deleted', 0, 'absent', true);
  end if;
  if current.user_id <> uid then
    return jsonb_build_object('ok', false, 'error',
      'that goal belongs to someone else');
  end if;

  with recursive tree as (
    select g.id from public.hc_goals g where g.id = p_goal_id
    union
    select c.id from public.hc_goals c join tree t on c.parent_id = t.id)
  select array_agg(id) into doomed from tree;

  delete from public.hc_goals g
   where g.id = any(doomed) and g.user_id = uid;
  get diagnostics removed = row_count;

  return jsonb_build_object('ok', true, 'deleted', removed);
end $$;

revoke all on function public.hc_delete_goal(uuid) from public;
grant execute on function public.hc_delete_goal(uuid) to authenticated;
