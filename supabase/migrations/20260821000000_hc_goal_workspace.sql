-- The goal workspace in Postgres: one project's chats, goals, TODO rows,
-- sources and related prompts, owned by one user and readable by no other.
--
-- Every table carries user_id rather than reaching through project_id to
-- find an owner. A policy that joins another table to decide a row's fate
-- is slower on every read and wrong in a way that is hard to see, so the
-- column is repeated on purpose.
--
-- Ids arrive from the client already decided (deterministic UUIDv5 under
-- the project's own UUID -- see project_sync.py), which is what lets a
-- sync be an upsert with no round trip to ask what is already there.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- projects

create table if not exists public.hc_projects (
  id            uuid primary key,
  user_id       uuid not null references auth.users (id) on delete cascade,
  cwd           text not null default '',
  name          text not null default '',
  objective     text not null default '',
  description   text not null default '',
  generated_at  timestamptz,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

create index if not exists hc_projects_user on public.hc_projects (user_id);

-- --------------------------------------------------------- project sources

create table if not exists public.hc_project_sources (
  id          uuid primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  local_id    text not null default '',
  type        text not null default '',
  label       text not null default '',
  position    integer not null default 0,
  unique (project_id, local_id)
);

create index if not exists hc_project_sources_project
  on public.hc_project_sources (project_id);

-- ------------------------------------------------------------------- chats

create table if not exists public.hc_chats (
  id            uuid primary key,
  user_id       uuid not null references auth.users (id) on delete cascade,
  project_id    uuid not null references public.hc_projects (id) on delete cascade,
  -- The Claude Code session id, kept as text: it is an identifier from
  -- another system, and a column that refuses a future format is a column
  -- that loses rows.
  session_id    text not null,
  created_at    timestamptz,
  updated_at    timestamptz,
  prompt_count  integer not null default 0,
  goal_count    integer not null default 0,
  unique (project_id, session_id)
);

create index if not exists hc_chats_project on public.hc_chats (project_id);

-- ------------------------------------------------------------------- goals

create table if not exists public.hc_goals (
  id            uuid primary key,
  user_id       uuid not null references auth.users (id) on delete cascade,
  project_id    uuid not null references public.hc_projects (id) on delete cascade,
  session_id    text not null,
  -- Unique only within its chat: two chats of one project each have a "g1".
  local_id      text not null,
  -- The whole of the hierarchy. Children, siblings, depth and the titles
  -- above are walks of this edge, not columns.
  parent_id     uuid references public.hc_goals (id) on delete set null,
  title         text not null default '',
  status        text not null default 'active',
  priority      text not null default 'normal',
  origin        text not null default '',
  description   text not null default '',
  notes         text not null default '',
  prompt        text not null default '',
  evidence_ids  text[] not null default '{}',
  important     jsonb not null default '[]'::jsonb,
  updated_at    timestamptz,
  unique (project_id, session_id, local_id),
  constraint hc_goals_status_known check (
    status in ('active', 'in_progress', 'completed', 'abandoned')),
  constraint hc_goals_not_own_parent check (parent_id is null or parent_id <> id)
);

create index if not exists hc_goals_project on public.hc_goals (project_id);
create index if not exists hc_goals_parent  on public.hc_goals (parent_id);
create index if not exists hc_goals_status  on public.hc_goals (user_id, status);

-- ------------------------------------------------------------------- todos

create table if not exists public.hc_todos (
  id          uuid primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  goal_id     uuid not null references public.hc_goals (id) on delete cascade,
  local_id    text not null,
  -- The rail's order is meaning: a parent row is the one above its
  -- children, and depth says how far in they sit.
  position    integer not null default 0,
  depth       integer not null default 0,
  text        text not null default '',
  status      text not null default '',
  question    text not null default '',
  unique (goal_id, local_id),
  constraint hc_todos_status_known check (
    status in ('', 'queued', 'building', 'asking', 'done', 'failed')),
  constraint hc_todos_depth_sane check (depth between 0 and 8)
);

create index if not exists hc_todos_goal on public.hc_todos (goal_id);
-- The question a build is waiting on, and the rows that failed: the two
-- reads the workspace makes across every project at once.
create index if not exists hc_todos_open
  on public.hc_todos (user_id, status) where status <> '';

-- ------------------------------------------------------------ goal sources

create table if not exists public.hc_goal_sources (
  id          uuid primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  goal_id     uuid not null references public.hc_goals (id) on delete cascade,
  local_id    text not null default '',
  type        text not null default '',
  label       text not null default '',
  position    integer not null default 0,
  unique (goal_id, local_id)
);

create index if not exists hc_goal_sources_goal on public.hc_goal_sources (goal_id);

-- --------------------------------------------------------- related prompts

create table if not exists public.hc_related_prompts (
  id          uuid primary key,
  user_id     uuid not null references auth.users (id) on delete cascade,
  project_id  uuid not null references public.hc_projects (id) on delete cascade,
  goal_id     uuid not null references public.hc_goals (id) on delete cascade,
  prompt_id   text not null,
  text        text not null default '',
  session_id  text not null default '',
  -- Whether inference linked this prompt or the reader marked it: the
  -- distinction the workspace shows, so it has to survive the trip.
  auto        boolean not null default false,
  created_at  timestamptz,
  position    integer not null default 0,
  unique (goal_id, prompt_id)
);

create index if not exists hc_related_prompts_goal
  on public.hc_related_prompts (goal_id);

-- ------------------------------------------------------------------- rls

alter table public.hc_projects        enable row level security;
alter table public.hc_project_sources enable row level security;
alter table public.hc_chats           enable row level security;
alter table public.hc_goals           enable row level security;
alter table public.hc_todos           enable row level security;
alter table public.hc_goal_sources    enable row level security;
alter table public.hc_related_prompts enable row level security;

-- Private to their owner, in both directions: a row cannot be read by
-- anyone else, and cannot be written with someone else's user_id either.
-- The with-check half is the one people leave off, and without it a client
-- may insert rows it will never be able to see.
do $$
declare t text;
begin
  foreach t in array array['hc_projects', 'hc_project_sources', 'hc_chats',
                           'hc_goals', 'hc_todos', 'hc_goal_sources',
                           'hc_related_prompts']
  loop
    execute format('drop policy if exists %I on public.%I', t || '_owner', t);
    execute format(
      'create policy %I on public.%I for all to authenticated '
      'using (user_id = (select auth.uid())) '
      'with check (user_id = (select auth.uid()))', t || '_owner', t);
  end loop;
end $$;

-- ------------------------------------------------------------------- sync

-- One project's whole state, in one transaction: upsert every row the
-- snapshot carries, then delete the rows of that project it does not.
--
-- The prune is the point. A snapshot says what exists; without deleting
-- what is missing from it, a goal removed locally lives in the remote for
-- good. Children go first so a parent is never orphaned mid-statement.
create or replace function public.hc_sync_project(payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  pid uuid := (payload ->> 'project_id')::uuid;
  removed jsonb;
begin
  if uid is null then
    raise exception 'hc_sync_project: no authenticated user';
  end if;
  if pid is null then
    raise exception 'hc_sync_project: payload has no project_id';
  end if;
  -- The snapshot names its owner; trust the session over the payload.
  if (payload ->> 'user_id') is distinct from uid::text then
    raise exception 'hc_sync_project: payload is not owned by the caller';
  end if;

  insert into public.hc_projects
    (id, user_id, cwd, name, objective, description, generated_at, updated_at)
  select (r ->> 'id')::uuid, uid, coalesce(r ->> 'cwd', ''),
         coalesce(r ->> 'name', ''), coalesce(r ->> 'objective', ''),
         coalesce(r ->> 'description', ''),
         (r ->> 'generated_at')::timestamptz, now()
  from jsonb_array_elements(payload -> 'projects') r
  on conflict (id) do update set
    cwd = excluded.cwd, name = excluded.name,
    objective = excluded.objective, description = excluded.description,
    generated_at = excluded.generated_at, updated_at = now();

  insert into public.hc_chats
    (id, user_id, project_id, session_id, created_at, updated_at,
     prompt_count, goal_count)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'session_id',
         (r ->> 'created_at')::timestamptz, (r ->> 'updated_at')::timestamptz,
         coalesce((r ->> 'prompt_count')::int, 0),
         coalesce((r ->> 'goal_count')::int, 0)
  from jsonb_array_elements(payload -> 'chats') r
  on conflict (id) do update set
    session_id = excluded.session_id, created_at = excluded.created_at,
    updated_at = excluded.updated_at,
    prompt_count = excluded.prompt_count, goal_count = excluded.goal_count;

  -- Parents before children: the self-reference is only satisfiable once
  -- the row it points at is in, and a snapshot lists goals in no order.
  insert into public.hc_goals
    (id, user_id, project_id, session_id, local_id, parent_id, title, status,
     priority, origin, description, notes, prompt, evidence_ids, important,
     updated_at)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'session_id', r ->> 'local_id',
         null, coalesce(r ->> 'title', ''),
         coalesce(r ->> 'status', 'active'),
         coalesce(r ->> 'priority', 'normal'), coalesce(r ->> 'origin', ''),
         coalesce(r ->> 'description', ''), coalesce(r ->> 'notes', ''),
         coalesce(r ->> 'prompt', ''),
         coalesce((select array_agg(value #>> '{}')
                   from jsonb_array_elements(r -> 'evidence_ids')), '{}'),
         coalesce(r -> 'important', '[]'::jsonb),
         (r ->> 'updated_at')::timestamptz
  from jsonb_array_elements(payload -> 'goals') r
  on conflict (id) do update set
    session_id = excluded.session_id, local_id = excluded.local_id,
    title = excluded.title, status = excluded.status,
    priority = excluded.priority, origin = excluded.origin,
    description = excluded.description, notes = excluded.notes,
    prompt = excluded.prompt, evidence_ids = excluded.evidence_ids,
    important = excluded.important, updated_at = excluded.updated_at;

  update public.hc_goals g set parent_id = (r ->> 'parent_id')::uuid
  from jsonb_array_elements(payload -> 'goals') r
  where g.id = (r ->> 'id')::uuid
    and g.parent_id is distinct from (r ->> 'parent_id')::uuid;

  insert into public.hc_todos
    (id, user_id, project_id, goal_id, local_id, position, depth, text,
     status, question)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         r ->> 'local_id', coalesce((r ->> 'position')::int, 0),
         coalesce((r ->> 'depth')::int, 0), coalesce(r ->> 'text', ''),
         coalesce(r ->> 'status', ''), coalesce(r ->> 'question', '')
  from jsonb_array_elements(payload -> 'todos') r
  on conflict (id) do update set
    local_id = excluded.local_id, position = excluded.position,
    depth = excluded.depth, text = excluded.text,
    status = excluded.status, question = excluded.question;

  insert into public.hc_goal_sources
    (id, user_id, project_id, goal_id, local_id, type, label, position)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         coalesce(r ->> 'local_id', ''), coalesce(r ->> 'type', ''),
         coalesce(r ->> 'label', ''), coalesce((r ->> 'position')::int, 0)
  from jsonb_array_elements(payload -> 'goal_sources') r
  on conflict (id) do update set
    local_id = excluded.local_id, type = excluded.type,
    label = excluded.label, position = excluded.position;

  insert into public.hc_project_sources
    (id, user_id, project_id, local_id, type, label, position)
  select (r ->> 'id')::uuid, uid, pid, coalesce(r ->> 'local_id', ''),
         coalesce(r ->> 'type', ''), coalesce(r ->> 'label', ''),
         coalesce((r ->> 'position')::int, 0)
  from jsonb_array_elements(payload -> 'project_sources') r
  on conflict (id) do update set
    local_id = excluded.local_id, type = excluded.type,
    label = excluded.label, position = excluded.position;

  insert into public.hc_related_prompts
    (id, user_id, project_id, goal_id, prompt_id, text, session_id, auto,
     created_at, position)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'goal_id')::uuid,
         r ->> 'prompt_id', coalesce(r ->> 'text', ''),
         coalesce(r ->> 'session_id', ''),
         coalesce((r ->> 'auto')::boolean, false),
         (r ->> 'created_at')::timestamptz,
         coalesce((r ->> 'position')::int, 0)
  from jsonb_array_elements(payload -> 'related_prompts') r
  on conflict (id) do update set
    prompt_id = excluded.prompt_id, text = excluded.text,
    session_id = excluded.session_id, auto = excluded.auto,
    created_at = excluded.created_at, position = excluded.position;

  -- What the snapshot no longer holds is gone from the workspace, so it
  -- goes from here too. Deepest first.
  with kept as (select (r ->> 'id')::uuid id
                from jsonb_array_elements(payload -> 'todos') r),
  gone as (delete from public.hc_todos t where t.project_id = pid
           and t.id not in (select id from kept) returning 1)
  select jsonb_build_object('todos', (select count(*) from gone)) into removed;

  delete from public.hc_goal_sources s where s.project_id = pid
    and s.id not in (select (r ->> 'id')::uuid
                     from jsonb_array_elements(payload -> 'goal_sources') r);
  delete from public.hc_related_prompts p where p.project_id = pid
    and p.id not in (select (r ->> 'id')::uuid
                     from jsonb_array_elements(payload -> 'related_prompts') r);
  delete from public.hc_goals g where g.project_id = pid
    and g.id not in (select (r ->> 'id')::uuid
                     from jsonb_array_elements(payload -> 'goals') r);
  delete from public.hc_chats c where c.project_id = pid
    and c.id not in (select (r ->> 'id')::uuid
                     from jsonb_array_elements(payload -> 'chats') r);
  delete from public.hc_project_sources s where s.project_id = pid
    and s.id not in (select (r ->> 'id')::uuid
                     from jsonb_array_elements(payload -> 'project_sources') r);

  return jsonb_build_object(
    'ok', true, 'project_id', pid,
    'goals', (select count(*) from public.hc_goals where project_id = pid),
    'todos', (select count(*) from public.hc_todos where project_id = pid),
    'pruned', removed);
end $$;

revoke all on function public.hc_sync_project(jsonb) from public;
grant execute on function public.hc_sync_project(jsonb) to authenticated;
