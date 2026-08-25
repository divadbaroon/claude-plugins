-- A code that lets someone in, rather than one that shows them the door.
--
-- The share table already mints tokens and keeps only their hashes. Until
-- now a token meant one thing: read this project anonymously. It gains a
-- second meaning here -- redeem this for a place on the roll -- which is
-- what makes a link an invitation.
--
-- The difference matters at the boundary. A 'view' token is a bearer
-- credential: whoever holds it reads, no account, no name. An 'invite'
-- token buys nothing on its own; it must be redeemed by someone signed in,
-- and what they get afterwards is tied to their account, not to the token.
-- So an invite that leaks is far less bad than a view token that leaks:
-- using it leaves a name on the roll, and the owner can strike it off.

alter table public.hc_project_shares
  add column if not exists kind text not null default 'view';
alter table public.hc_project_shares
  add column if not exists role text not null default 'reader';
alter table public.hc_project_shares
  add column if not exists max_uses integer;

do $$
begin
  if not exists (select 1 from pg_constraint
                  where conname = 'hc_shares_kind_known') then
    alter table public.hc_project_shares
      add constraint hc_shares_kind_known check (kind in ('view', 'invite'));
  end if;
  if not exists (select 1 from pg_constraint
                  where conname = 'hc_shares_role_known') then
    alter table public.hc_project_shares
      add constraint hc_shares_role_known check (role in ('reader', 'editor'));
  end if;
end $$;

drop function if exists public.hc_create_share(uuid, text, integer, boolean);

create or replace function public.hc_create_share(
  p_project_id uuid,
  p_label text default '',
  p_expires_in_days integer default null,
  p_kind text default 'invite',
  p_role text default 'reader',
  p_max_uses integer default null)
returns jsonb
language plpgsql
security invoker
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
                  where p.id = p_project_id and p.user_id = uid) then
    raise exception 'hc_create_share: no such project of yours';
  end if;
  if coalesce(p_kind, 'invite') not in ('view', 'invite') then
    raise exception 'hc_create_share: kind must be view or invite';
  end if;
  if coalesce(p_role, 'reader') not in ('reader', 'editor') then
    raise exception 'hc_create_share: role must be reader or editor';
  end if;
  token := 'hcs_' || encode(gen_random_bytes(32), 'hex');
  insert into public.hc_project_shares
    (project_id, user_id, token_hash, label, kind, role, can_write,
     max_uses, expires_at)
  values (p_project_id, uid, encode(digest(token, 'sha256'), 'hex'),
          coalesce(p_label, ''), coalesce(p_kind, 'invite'),
          coalesce(p_role, 'reader'),
          coalesce(p_role, 'reader') = 'editor',
          p_max_uses,
          case when p_expires_in_days is null then null
               else now() + make_interval(days => p_expires_in_days) end)
  returning id into row_id;
  return jsonb_build_object('ok', true, 'id', row_id, 'token', token,
                            'kind', coalesce(p_kind, 'invite'),
                            'role', coalesce(p_role, 'reader'));
end $$;

-- Redeeming: the caller must be signed in, and what they get is written
-- against their own account. The token is spent in the sense that it is
-- counted, not consumed -- an invite may be handed to a team.
create or replace function public.hc_redeem_share(p_token text)
returns jsonb
language plpgsql
security definer
set search_path = public, extensions, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  share public.hc_project_shares;
  proj public.hc_projects;
begin
  if uid is null then
    return jsonb_build_object('ok', false, 'error',
      'sign in first: an invite is joined to an account, not held on its own');
  end if;
  if p_token is null or length(p_token) < 16 then
    return jsonb_build_object('ok', false, 'error', 'no code');
  end if;
  select * into share from public.hc_project_shares s
   where s.token_hash = encode(digest(p_token, 'sha256'), 'hex') limit 1;
  -- One answer for never-was, revoked, expired and spent.
  if share.id is null
     or share.revoked_at is not null
     or (share.expires_at is not null and share.expires_at <= now())
     or (share.max_uses is not null and share.uses >= share.max_uses) then
    return jsonb_build_object('ok', false, 'error', 'this invite is not open');
  end if;
  if share.kind <> 'invite' then
    return jsonb_build_object('ok', false, 'error',
      'that code is a view link, not an invitation');
  end if;

  select * into proj from public.hc_projects p where p.id = share.project_id;
  if proj.user_id = uid then
    return jsonb_build_object('ok', true, 'already', true,
      'project_id', proj.id, 'name', proj.name, 'role', 'owner');
  end if;

  insert into public.hc_project_members (project_id, user_id, role, added_by)
  values (share.project_id, uid, share.role, share.user_id)
  on conflict (project_id, user_id) do update set role = excluded.role;

  update public.hc_project_shares s
     set uses = s.uses + 1, last_used_at = now()
   where s.id = share.id;

  return jsonb_build_object('ok', true, 'project_id', proj.id,
                            'name', proj.name, 'role', share.role);
end $$;

revoke all on function public.hc_create_share(uuid, text, integer, text, text, integer) from public;
revoke all on function public.hc_redeem_share(text) from public;
grant execute on function public.hc_create_share(uuid, text, integer, text, text, integer) to authenticated;
grant execute on function public.hc_redeem_share(text) to authenticated;
