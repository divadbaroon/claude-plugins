-- Two people, one project: the collaborator signs in as themselves.
--
-- This is the other half of sharing, and the better half when the reader
-- has the workspace installed. No token, no bearer credential, no page to
-- host: they point their own workspace at this project, sign in with their
-- own account, and row security hands them what they are a member of.
--
-- THE IMPORTANT PART is that reading and writing widen separately. The
-- existing policies are FOR ALL, and `using` governs DELETE as much as
-- SELECT -- so simply widening them would let a reader delete the owner's
-- rows. Instead the owner keeps one FOR ALL policy, and members get a
-- second, SELECT-only one. Postgres ORs permissive policies together, so
-- reads become "mine or shared with me" while writes stay "mine".

create table if not exists public.hc_project_members (
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade,
  -- 'reader' today. 'editor' is where write access will live, and it waits
  -- on an answer to what happens when two people change one goal.
  role        text not null default 'reader',
  added_at    timestamptz not null default now(),
  added_by    uuid references auth.users (id) on delete set null,
  primary key (project_id, user_id),
  constraint hc_members_role_known check (role in ('reader', 'editor'))
);

create index if not exists hc_members_user on public.hc_project_members (user_id);

alter table public.hc_project_members enable row level security;

-- The projects the caller is a member of. SECURITY DEFINER on purpose: the
-- policies below call it, and a policy that reads a table with its own
-- policy invites recursion and costs a plan on every row. STABLE so the
-- planner calls it once per statement.
create or replace function public.hc_member_projects()
returns setof uuid
language sql
security definer
stable
set search_path = public
as $$
  select m.project_id from public.hc_project_members m
   where m.user_id = (select auth.uid())
$$;

-- Owners manage the roll; a member may see their own row on it and no more.
-- This reads hc_projects, never hc_project_members, so it cannot recurse.
drop policy if exists hc_members_owner on public.hc_project_members;
create policy hc_members_owner on public.hc_project_members
  for all to authenticated
  using (project_id in (select p.id from public.hc_projects p
                         where p.user_id = (select auth.uid())))
  with check (project_id in (select p.id from public.hc_projects p
                              where p.user_id = (select auth.uid())));

drop policy if exists hc_members_self on public.hc_project_members;
create policy hc_members_self on public.hc_project_members
  for select to authenticated
  using (user_id = (select auth.uid()));

-- ------------------------------------------------- reads widen, writes do not

-- hc_projects is keyed by `id` rather than `project_id`; the rest follow one
-- shape, so they are done in a loop and it is spelled out once.
drop policy if exists hc_projects_member_read on public.hc_projects;
create policy hc_projects_member_read on public.hc_projects
  for select to authenticated
  using (id in (select public.hc_member_projects()));

do $$
declare t text;
begin
  foreach t in array array['hc_project_sources', 'hc_chats', 'hc_goals',
                           'hc_todos', 'hc_goal_sources', 'hc_related_prompts']
  loop
    execute format('drop policy if exists %I on public.%I', t || '_member_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated '
      'using (project_id in (select public.hc_member_projects()))',
      t || '_member_read', t);
  end loop;
end $$;

-- ------------------------------------------------------------- the roll

-- Invited by email, because a UUID is not something one person can read off
-- another. SECURITY DEFINER because auth.users is not readable by anyone
-- here -- and it returns nothing from that table but the id it needed.
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
    -- Said plainly: the person has to exist here before they can be added,
    -- and the owner is the one who can create them.
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
  on conflict (project_id, user_id)
    do update set role = excluded.role;
  return jsonb_build_object('ok', true, 'user_id', who);
end $$;

create or replace function public.hc_remove_member(project_id uuid, member uuid)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare uid uuid := (select auth.uid());
begin
  if uid is null then
    raise exception 'hc_remove_member: no authenticated user';
  end if;
  delete from public.hc_project_members m
   where m.project_id = hc_remove_member.project_id
     and m.user_id = hc_remove_member.member
     and m.project_id in (select p.id from public.hc_projects p
                           where p.user_id = uid);
  return jsonb_build_object('ok', found);
end $$;

-- Who is on a project, with the emails the owner invited them by. The
-- owner's own listing only: the check is inside, because the function sees
-- past policies.
create or replace function public.hc_list_members(project_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare uid uuid := (select auth.uid());
begin
  if uid is null then
    raise exception 'hc_list_members: no authenticated user';
  end if;
  if not exists (select 1 from public.hc_projects p
                  where p.id = hc_list_members.project_id and p.user_id = uid) then
    raise exception 'hc_list_members: no such project of yours';
  end if;
  return coalesce((
    select jsonb_agg(jsonb_build_object(
             'user_id', m.user_id, 'email', u.email,
             'role', m.role, 'added_at', m.added_at))
      from public.hc_project_members m
      join auth.users u on u.id = m.user_id
     where m.project_id = hc_list_members.project_id), '[]'::jsonb);
end $$;

revoke all on function public.hc_member_projects() from public;
revoke all on function public.hc_add_member(uuid, text, text) from public;
revoke all on function public.hc_remove_member(uuid, uuid) from public;
revoke all on function public.hc_list_members(uuid) from public;
grant execute on function public.hc_member_projects() to authenticated;
grant execute on function public.hc_add_member(uuid, text, text) to authenticated;
grant execute on function public.hc_remove_member(uuid, uuid) to authenticated;
grant execute on function public.hc_list_members(uuid) to authenticated;
