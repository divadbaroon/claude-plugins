-- Handing one project to someone who has no account here.
--
-- The reader presents a token and nothing else: no sign-in, no user, no row
-- of their own. That is the whole point, and it is also the danger, so the
-- shape is deliberate:
--
-- * The token is never stored. Only its SHA-256 lands in the table, so a
--   leak of the table is not a leak of anyone's access -- the same reason a
--   password file holds hashes.
-- * RLS on the share table stays owner-only, exactly like every other table.
--   The reader never selects from it; they cannot see that it exists.
-- * One SECURITY DEFINER function is the only door, and it opens onto one
--   project -- the one its token names. It is not a general read: there is
--   no way to ask it for a project you do not hold a token for.
--
-- The alternative -- teaching each table's policy to read a token out of
-- request.headers -- was rejected on purpose. It spreads the check across
-- seven policies, and a mistake in any one of them is a silent leak.

-- ------------------------------------------------------------------ table

create table if not exists public.hc_project_shares (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  user_id     uuid not null references auth.users (id) on delete cascade,
  -- Hex SHA-256 of the token. The token itself is shown once, at creation,
  -- and is not recoverable from here.
  token_hash  text not null unique,
  label       text not null default '',
  can_write   boolean not null default false,
  created_at  timestamptz not null default now(),
  expires_at  timestamptz,
  revoked_at  timestamptz,
  last_used_at timestamptz,
  uses        bigint not null default 0
);

create index if not exists hc_shares_project on public.hc_project_shares (project_id);
create index if not exists hc_shares_hash on public.hc_project_shares (token_hash);

alter table public.hc_project_shares enable row level security;

drop policy if exists hc_project_shares_owner on public.hc_project_shares;
create policy hc_project_shares_owner on public.hc_project_shares
  for all to authenticated
  using (user_id = (select auth.uid()))
  with check (user_id = (select auth.uid()));

-- ------------------------------------------------------------- minting

-- Returns the token ONCE. Nothing afterwards can recover it: lose it and
-- mint another. A share belongs to the caller and to a project the caller
-- owns -- both checked here rather than trusted from the argument.
create or replace function public.hc_create_share(
  project_id uuid,
  label text default '',
  expires_in_days integer default null,
  can_write boolean default false)
returns jsonb
language plpgsql
security invoker
-- pgcrypto lives in `extensions` on Supabase and in `public` on a plain
-- install; naming both resolves digest()/gen_random_bytes() either way.
-- An explicit search_path is not optional on a definer-adjacent function:
-- without one, the caller chooses which `digest` runs.
set search_path = public, extensions, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  token text;
  row_id uuid;
begin
  if uid is null then
    raise exception 'hc_create_share: no authenticated user';
  end if;
  if not exists (select 1 from public.hc_projects p
                 where p.id = hc_create_share.project_id and p.user_id = uid) then
    raise exception 'hc_create_share: no such project of yours';
  end if;
  -- 32 bytes of randomness, hex. Long enough that guessing is not a plan.
  token := 'hcs_' || encode(gen_random_bytes(32), 'hex');
  insert into public.hc_project_shares
    (project_id, user_id, token_hash, label, can_write, expires_at)
  values (hc_create_share.project_id, uid,
          encode(digest(token, 'sha256'), 'hex'),
          coalesce(hc_create_share.label, ''),
          coalesce(hc_create_share.can_write, false),
          case when hc_create_share.expires_in_days is null then null
               else now() + make_interval(days => hc_create_share.expires_in_days) end)
  returning id into row_id;
  return jsonb_build_object('ok', true, 'id', row_id, 'token', token);
end $$;

create or replace function public.hc_revoke_share(share_id uuid)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare uid uuid := (select auth.uid());
begin
  if uid is null then
    raise exception 'hc_revoke_share: no authenticated user';
  end if;
  update public.hc_project_shares s set revoked_at = now()
   where s.id = hc_revoke_share.share_id and s.user_id = uid
     and s.revoked_at is null;
  return jsonb_build_object('ok', found);
end $$;

-- --------------------------------------------------------------- reading

-- The one door. SECURITY DEFINER, so it sees past the owner-only policies
-- -- which is exactly why it must never take anything but a token, and must
-- never return more than the project that token names.
create or replace function public.hc_read_shared(token text)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  share public.hc_project_shares;
  pid uuid;
  doc jsonb;
begin
  if token is null or length(token) < 16 then
    return jsonb_build_object('ok', false, 'error', 'no token');
  end if;
  select * into share from public.hc_project_shares s
   where s.token_hash = encode(digest(token, 'sha256'), 'hex')
   limit 1;
  -- One answer for "never existed", "revoked" and "expired". Telling them
  -- apart tells a stranger which of their guesses was once real.
  if share.id is null
     or share.revoked_at is not null
     or (share.expires_at is not null and share.expires_at <= now()) then
    return jsonb_build_object('ok', false, 'error', 'this share is not open');
  end if;
  pid := share.project_id;

  update public.hc_project_shares s
     set uses = s.uses + 1, last_used_at = now()
   where s.id = share.id;

  select jsonb_build_object(
    'ok', true,
    'read_only', not share.can_write,
    'label', share.label,
    'project', (select to_jsonb(p) - 'user_id'
                  from public.hc_projects p where p.id = pid),
    'project_sources', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_project_sources x where x.project_id = pid), '[]'::jsonb),
    'chats', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_chats x where x.project_id = pid), '[]'::jsonb),
    'goals', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_goals x where x.project_id = pid), '[]'::jsonb),
    'todos', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_todos x where x.project_id = pid), '[]'::jsonb),
    'goal_sources', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_goal_sources x where x.project_id = pid), '[]'::jsonb),
    'related_prompts', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_related_prompts x where x.project_id = pid), '[]'::jsonb)
  ) into doc;
  -- The owner's id is stripped from every row above: a reader is being
  -- handed a project, not an account to go looking for.
  return doc;
end $$;

revoke all on function public.hc_create_share(uuid, text, integer, boolean) from public;
revoke all on function public.hc_revoke_share(uuid) from public;
revoke all on function public.hc_read_shared(text) from public;
grant execute on function public.hc_create_share(uuid, text, integer, boolean) to authenticated;
grant execute on function public.hc_revoke_share(uuid) to authenticated;
-- The reader has no account: they arrive as anon, holding a token.
grant execute on function public.hc_read_shared(text) to anon, authenticated;
