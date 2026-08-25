-- The owner could not see what was contributed to their own project.
--
-- Reads widened for members -- "projects I am on the roll of" -- but an
-- owner is not on their own roll, so the widening never applied to them.
-- The contributor saw eighteen goals and the owner saw sixteen: the two
-- halves of one shared tree, each invisible to the wrong person.
--
-- Still SELECT only. An owner seeing a contributor's goal is not the same
-- as being able to edit it, and co-editing one row remains a question with
-- no answer yet.

do $$
declare t text;
begin
  foreach t in array array['hc_project_sources', 'hc_chats', 'hc_goals',
                           'hc_todos', 'hc_goal_sources', 'hc_related_prompts']
  loop
    execute format('drop policy if exists %I on public.%I', t || '_owner_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated '
      'using (project_id in (select p.id from public.hc_projects p '
      '                       where p.user_id = (select auth.uid())))',
      t || '_owner_read', t);
  end loop;
end $$;

-- When a contributor leaves, their goals stay: nothing else may delete
-- another person's rows, and that is the rule worth keeping. This is the
-- owner's deliberate exception, asked for by name, for their own project.
create or replace function public.hc_purge_member(
  p_project_id uuid, p_member uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  gone integer;
begin
  if uid is null then
    raise exception 'hc_purge_member: no authenticated user';
  end if;
  if not exists (select 1 from public.hc_projects p
                  where p.id = p_project_id and p.user_id = uid) then
    raise exception 'hc_purge_member: no such project of yours';
  end if;
  if p_member = uid then
    raise exception 'hc_purge_member: that would delete your own work';
  end if;
  delete from public.hc_todos
   where project_id = p_project_id and user_id = p_member;
  delete from public.hc_goal_sources
   where project_id = p_project_id and user_id = p_member;
  delete from public.hc_related_prompts
   where project_id = p_project_id and user_id = p_member;
  delete from public.hc_goals
   where project_id = p_project_id and user_id = p_member;
  get diagnostics gone = row_count;
  delete from public.hc_chats
   where project_id = p_project_id and user_id = p_member;
  delete from public.hc_project_members
   where project_id = p_project_id and user_id = p_member;
  return jsonb_build_object('ok', true, 'goals_removed', gone);
end $$;

revoke all on function public.hc_purge_member(uuid, uuid) from public;
grant execute on function public.hc_purge_member(uuid, uuid) to authenticated;
