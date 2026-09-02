-- A fourth register, and what the reader already knows: the web onboarding
-- grades short answers per area and the workspace pitches every prompt at
-- those levels. Kept on hc_profiles beside the other four answers.

alter table public.hc_profiles
  add column if not exists knowledge jsonb not null default '[]'::jsonb;

alter table public.hc_profiles
  drop constraint if exists hc_profiles_tech_level_known;
alter table public.hc_profiles
  add constraint hc_profiles_tech_level_known
  check (tech_level in ('', 'plain', 'some', 'full', 'expert'));

drop function if exists public.hc_set_profile(text, text, text, text);

create or replace function public.hc_set_profile(
  p_name text, p_year text, p_major text, p_level text,
  p_knowledge jsonb default '[]'::jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid   uuid := (select auth.uid());
  level text := btrim(coalesce(p_level, ''));
begin
  if uid is null then
    raise exception 'hc_set_profile: no authenticated user';
  end if;
  if level not in ('', 'plain', 'some', 'full', 'expert') then
    level := '';
  end if;
  insert into public.hc_profiles
         (user_id, display_name, year, major, tech_level, knowledge, updated_at)
  values (uid,
          left(btrim(coalesce(p_name, '')), 60),
          left(btrim(coalesce(p_year, '')), 40),
          left(btrim(coalesce(p_major, '')), 80),
          level,
          case when jsonb_typeof(p_knowledge) = 'array' then p_knowledge else '[]'::jsonb end,
          now())
  on conflict (user_id) do update
    set display_name = excluded.display_name,
        year         = excluded.year,
        major        = excluded.major,
        tech_level   = excluded.tech_level,
        knowledge    = excluded.knowledge,
        updated_at   = now();
  return jsonb_build_object(
    'ok', true,
    'profile', (select jsonb_build_object(
                         'name', p.display_name, 'year', p.year,
                         'major', p.major, 'level', p.tech_level,
                         'knowledge', p.knowledge)
                  from public.hc_profiles p where p.user_id = uid));
end $$;

revoke all on function public.hc_set_profile(text, text, text, text, jsonb) from public;
grant execute on function public.hc_set_profile(text, text, text, text, jsonb) to authenticated;
