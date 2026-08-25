-- What to call each other.
--
-- A goal in a shared tree needs an author, and until now that was scraped
-- from the email -- which gives "dbarron410", not "David". A name is a
-- thing people choose, so it is asked for and stored.
--
-- Kept out of auth.users on purpose. That table belongs to the auth system,
-- writing to it needs privileges no workspace should hold, and a profile is
-- ours to shape. One row per account, and the account writes its own.

create table if not exists public.hc_profiles (
  user_id      uuid primary key references auth.users (id) on delete cascade,
  display_name text not null default '',
  updated_at   timestamptz not null default now()
);

alter table public.hc_profiles enable row level security;

-- Your own row, yours alone. Other people's names do not come from here --
-- reading the table would say who else has an account, which is not the
-- question being asked. They come from the function below, which answers
-- only about people you already share a project with.
drop policy if exists hc_profiles_self on public.hc_profiles;
create policy hc_profiles_self on public.hc_profiles
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

create or replace function public.hc_set_display_name(p_name text)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare uid uuid := (select auth.uid());
begin
  if uid is null then
    raise exception 'hc_set_display_name: no authenticated user';
  end if;
  insert into public.hc_profiles (user_id, display_name, updated_at)
  values (uid, btrim(coalesce(p_name, ''))::text, now())
  on conflict (user_id) do update
    set display_name = excluded.display_name, updated_at = now();
  return jsonb_build_object('ok', true,
    'display_name', (select display_name from public.hc_profiles
                      where user_id = uid));
end $$;

-- Everyone on one project, with what to call them. SECURITY DEFINER so it
-- can see past the profile table's own policy -- and it is safe to, because
-- it answers only for a project the caller is already on, and returns only
-- a name. A member gets names without being shown the roll's emails; the
-- owner, who invited them by email, gets those too.
create or replace function public.hc_project_people(p_project_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  owner_id uuid;
  is_owner boolean;
begin
  if uid is null then
    raise exception 'hc_project_people: no authenticated user';
  end if;
  select p.user_id into owner_id from public.hc_projects p
   where p.id = p_project_id;
  if owner_id is null then
    return '[]'::jsonb;
  end if;
  is_owner := owner_id = uid;
  if not is_owner and not exists (
        select 1 from public.hc_project_members m
         where m.project_id = p_project_id and m.user_id = uid) then
    return '[]'::jsonb;
  end if;
  return coalesce((
    select jsonb_agg(jsonb_build_object(
             'user_id', person.id,
             'display_name', coalesce(pr.display_name, ''),
             'email', case when is_owner then u.email else null end,
             'role', person.role))
      from (
        select owner_id as id, 'owner'::text as role
        union all
        select m.user_id, m.role from public.hc_project_members m
         where m.project_id = p_project_id
      ) person
      join auth.users u on u.id = person.id
      left join public.hc_profiles pr on pr.user_id = person.id
  ), '[]'::jsonb);
end $$;

revoke all on function public.hc_set_display_name(text) from public;
revoke all on function public.hc_project_people(uuid) from public;
grant execute on function public.hc_set_display_name(text) to authenticated;
grant execute on function public.hc_project_people(uuid) to authenticated;
