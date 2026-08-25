-- hc_add_member could not be run: its parameters were named after the
-- columns they were inserted into, and `on conflict (project_id, user_id)`
-- then had two things to mean. Postgres refuses to rename a parameter in
-- place, so the function is dropped and rebuilt with prefixed names.
--
-- The names are part of the call: PostgREST passes arguments by name, so
-- the client sends p_project_id / p_email / p_role now.

drop function if exists public.hc_add_member(uuid, text, text);

create or replace function public.hc_add_member(
  p_project_id uuid, p_email text, p_role text default 'reader')
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  who uuid;
begin
  if uid is null then
    raise exception 'hc_add_member: no authenticated user';
  end if;
  if not exists (select 1 from public.hc_projects p
                  where p.id = p_project_id and p.user_id = uid) then
    raise exception 'hc_add_member: no such project of yours';
  end if;
  if coalesce(p_role, 'reader') not in ('reader', 'editor') then
    raise exception 'hc_add_member: role must be reader or editor';
  end if;
  select u.id into who from auth.users u
   where lower(u.email) = lower(trim(p_email)) limit 1;
  if who is null then
    return jsonb_build_object('ok', false, 'error',
      'no account with that email in this project yet -- add them under '
      || 'Authentication first');
  end if;
  if who = uid then
    return jsonb_build_object('ok', false, 'error',
      'that is your own account; you already own this project');
  end if;
  insert into public.hc_project_members (project_id, user_id, role, added_by)
  values (p_project_id, who, coalesce(p_role, 'reader'), uid)
  on conflict (project_id, user_id) do update set role = excluded.role;
  return jsonb_build_object('ok', true, 'user_id', who);
end $$;

revoke all on function public.hc_add_member(uuid, text, text) from public;
grant execute on function public.hc_add_member(uuid, text, text) to authenticated;
