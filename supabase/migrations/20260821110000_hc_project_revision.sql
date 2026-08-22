-- Asking "has anything changed?" without fetching everything to find out.
--
-- An open shared workspace polls, and each poll was rebuilding the whole
-- project: seven selects and better than a second, for an answer that is
-- almost always "no, nothing". This is the cheap question -- counts and the
-- latest timestamp, in one round trip -- so the expensive one is only asked
-- when the answer has actually moved.
--
-- TODO rows had no updated_at, so editing a row's text changed nothing a
-- count could see. They get one, and the sync sets it, or a poll would
-- happily report "unchanged" over an edit.

alter table public.hc_todos
  add column if not exists updated_at timestamptz not null default now();

create or replace function public.hc_project_revision(p_project_id uuid)
returns jsonb
language sql
security invoker
stable
set search_path = public
as $$
  select jsonb_build_object(
    'goals',    (select count(*) from public.hc_goals
                  where project_id = p_project_id),
    'goals_at', (select max(updated_at) from public.hc_goals
                  where project_id = p_project_id),
    'todos',    (select count(*) from public.hc_todos
                  where project_id = p_project_id),
    'todos_at', (select max(updated_at) from public.hc_todos
                  where project_id = p_project_id),
    'chats',    (select count(*) from public.hc_chats
                  where project_id = p_project_id),
    'sources',  (select count(*) from public.hc_goal_sources
                  where project_id = p_project_id),
    'prompts',  (select count(*) from public.hc_related_prompts
                  where project_id = p_project_id),
    'project',  (select max(updated_at) from public.hc_projects
                  where id = p_project_id))
$$;

-- The sync must stamp rows it changes, or the revision cannot see the edit.
create or replace function public.hc_touch_todo()
returns trigger
language plpgsql
as $$
begin
  new.updated_at := now();
  return new;
end $$;

drop trigger if exists hc_todos_touched on public.hc_todos;
create trigger hc_todos_touched
  before update on public.hc_todos
  for each row execute function public.hc_touch_todo();

revoke all on function public.hc_project_revision(uuid) from public;
grant execute on function public.hc_project_revision(uuid) to authenticated;
