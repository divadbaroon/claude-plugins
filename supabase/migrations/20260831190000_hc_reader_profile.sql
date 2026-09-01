-- Who is at the keyboard, and how much jargon they want back.
--
-- The setup page asks four things before anything else happens: a name, a
-- year, what they study, and how technical explanations should be. The
-- answers are appended to every prompt the tool sends, so they belong to
-- the account rather than to a chat or a project -- the same person reads
-- the same way in all of them.
--
-- Kept on hc_profiles, which already holds the one thing of this kind we
-- store: what to call somebody. `display_name` IS the name asked for here,
-- so nothing new is added for it -- a second name column would be a second
-- answer to "what is this person called".

alter table public.hc_profiles
  add column if not exists year       text not null default '',
  add column if not exists major      text not null default '',
  add column if not exists tech_level text not null default '';

-- Three stops and nothing else. A level the workspace does not recognise
-- would be appended to a prompt as an instruction nobody wrote, so the
-- column refuses it here rather than downstream.
alter table public.hc_profiles
  drop constraint if exists hc_profiles_tech_level_known;
alter table public.hc_profiles
  add constraint hc_profiles_tech_level_known
  check (tech_level in ('', 'plain', 'some', 'full'));

-- One write for all four, because the page asks them together and a
-- half-saved profile is a reader who is addressed by name in the register
-- of somebody else.
create or replace function public.hc_set_profile(
  p_name text, p_year text, p_major text, p_level text)
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
  if level not in ('', 'plain', 'some', 'full') then
    level := '';
  end if;
  insert into public.hc_profiles
         (user_id, display_name, year, major, tech_level, updated_at)
  values (uid,
          left(btrim(coalesce(p_name, '')), 60),
          left(btrim(coalesce(p_year, '')), 40),
          left(btrim(coalesce(p_major, '')), 80),
          level,
          now())
  on conflict (user_id) do update
    set display_name = excluded.display_name,
        year         = excluded.year,
        major        = excluded.major,
        tech_level   = excluded.tech_level,
        updated_at   = now();
  return jsonb_build_object(
    'ok', true,
    'profile', (select jsonb_build_object(
                         'name', p.display_name, 'year', p.year,
                         'major', p.major, 'level', p.tech_level)
                  from public.hc_profiles p where p.user_id = uid));
end $$;

revoke all on function public.hc_set_profile(text, text, text, text) from public;
grant execute on function public.hc_set_profile(text, text, text, text)
  to authenticated;
