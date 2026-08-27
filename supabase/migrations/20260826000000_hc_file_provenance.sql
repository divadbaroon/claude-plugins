-- What the work touched: files, the runs that changed them, and the few
-- excerpts worth reading.
--
-- Until now a goal said what was intended and a TODO row said what was
-- done, and the artifact of the doing -- the files -- lived only on the
-- machine that made them. A collaborator reading a shared project could see
-- "rewrite the bridge" and had no way to learn which file that was.
--
-- Three questions, three tables, kept apart on purpose:
--
--   hc_files         what exists, and what state it is in (metadata only)
--   hc_runs          one agent run, with the commit range it spanned
--   hc_run_files     which files that run touched, and by how much
--   hc_file_excerpts the specific lines someone should actually read
--
-- CONTENT IS THE ODD ONE OUT. The first three carry paths, hashes, counts
-- and SHAs -- facts about files, never files. `hc_file_excerpts` is the one
-- place a copy of the user's source leaves their disk, which is why it is a
-- separate table with its own capture rule rather than a `content` column on
-- hc_files: a project can hold the whole provenance graph with that table
-- empty, and the client decides per project whether to fill it.
--
-- Ids arrive decided, as everywhere else here: deterministic UUIDv5 under
-- the project's namespace, so a re-sync is an upsert and not a duplicate.

-- ------------------------------------------------------------------- files

create table if not exists public.hc_files (
  id             uuid primary key,
  user_id        uuid not null references auth.users (id) on delete cascade,
  project_id     uuid not null references public.hc_projects (id) on delete cascade,
  -- Relative to the project's cwd when the file sits under it, absolute
  -- when it does not. Either way it is the name the reader would type.
  path           text not null,
  name           text not null default '',
  ext            text not null default '',
  size_bytes     bigint,
  modified_at    timestamptz,
  -- SHA-256 of the bytes. Identity without the bytes: two machines can
  -- agree a file is the same file, and a stale excerpt can be spotted,
  -- without anything of its content being stored.
  content_sha256 text not null default '',
  git_tracked    boolean not null default false,
  -- Porcelain, flattened to a word: 'clean', 'modified', 'staged',
  -- 'untracked', 'ignored', 'deleted', or '' when the project is not a
  -- repository at all -- which many of these projects are not.
  git_status     text not null default '',
  git_blob_sha   text not null default '',
  last_commit_sha text not null default '',
  last_commit_at  timestamptz,
  -- Cumulative across every run that touched it: the cheap answer to
  -- "which files is this project actually about".
  edits          integer not null default 0,
  -- A file the project has known and no longer has. Kept rather than
  -- deleted: a run that edited it is still true, and a foreign key that
  -- vanishes takes that history with it.
  missing        boolean not null default false,
  binary_file    boolean not null default false,
  first_seen_at  timestamptz not null default now(),
  last_seen_at   timestamptz not null default now(),
  unique (project_id, path),
  constraint hc_files_status_known check (
    git_status in ('', 'clean', 'modified', 'staged', 'untracked',
                   'ignored', 'deleted'))
);

create index if not exists hc_files_project on public.hc_files (project_id);
-- "What did we work on most" -- the one ordering the pane reads.
create index if not exists hc_files_edits
  on public.hc_files (project_id, edits desc);

-- -------------------------------------------------------------------- runs

create table if not exists public.hc_runs (
  id              uuid primary key,
  user_id         uuid not null references auth.users (id) on delete cascade,
  project_id      uuid not null references public.hc_projects (id) on delete cascade,
  -- The Claude session the run happened in, and the goal it was launched
  -- against. The goal reference is nullable and ON DELETE SET NULL: a run
  -- outlives the goal that prompted it, and losing the goal should not
  -- erase the record of the work.
  session_id      text not null,
  goal_id         uuid references public.hc_goals (id) on delete set null,
  status          text not null default '',
  state           text not null default '',
  started_at      timestamptz,
  finished_at     timestamptz,
  cwd             text not null default '',
  git_branch      text not null default '',
  -- The commit range. Two SHAs and the diff between them is the whole
  -- change history of the run, recoverable by anyone holding the repo --
  -- which is why the diff itself is not stored, only its shape.
  git_head_before text not null default '',
  git_head_after  text not null default '',
  committed       boolean not null default false,
  summary         text not null default '',
  task_total      integer not null default 0,
  files_total     integer not null default 0,
  updated_at      timestamptz not null default now(),
  unique (project_id, session_id)
);

create index if not exists hc_runs_project on public.hc_runs (project_id);
create index if not exists hc_runs_goal on public.hc_runs (goal_id);

-- --------------------------------------------------------------- run files

create table if not exists public.hc_run_files (
  id            uuid primary key,
  user_id       uuid not null references auth.users (id) on delete cascade,
  project_id    uuid not null references public.hc_projects (id) on delete cascade,
  run_id        uuid not null references public.hc_runs (id) on delete cascade,
  file_id       uuid not null references public.hc_files (id) on delete cascade,
  -- Denormalized so a reader can list a run's changes without a join, and
  -- so the row still says something if the file row is later pruned.
  path          text not null default '',
  tool          text not null default '',
  edits         integer not null default 0,
  -- Null rather than zero when git could not be asked: "we do not know" and
  -- "nothing changed" are different answers and a reader deserves both.
  lines_added   integer,
  lines_removed integer,
  commit_sha    text not null default '',
  first_at      timestamptz,
  last_at       timestamptz,
  unique (run_id, file_id)
);

create index if not exists hc_run_files_run on public.hc_run_files (run_id);
create index if not exists hc_run_files_file on public.hc_run_files (file_id);

-- ---------------------------------------------------------------- excerpts

create table if not exists public.hc_file_excerpts (
  id             uuid primary key,
  user_id        uuid not null references auth.users (id) on delete cascade,
  project_id     uuid not null references public.hc_projects (id) on delete cascade,
  file_id        uuid not null references public.hc_files (id) on delete cascade,
  goal_id        uuid references public.hc_goals (id) on delete set null,
  run_id         uuid references public.hc_runs (id) on delete set null,
  start_line     integer not null default 1,
  end_line       integer not null default 1,
  content        text not null default '',
  -- The file's hash when this excerpt was taken. The file moves on; this
  -- says whether what is quoted is still what is there.
  file_sha256    text not null default '',
  -- Why this passage and not another. Written by whatever selected it, and
  -- shown to the reader -- an unexplained excerpt is a fragment.
  reason         text not null default '',
  -- 'run' (it changed here), 'goal' (evidence names it), 'reader' (someone
  -- marked it). Kept because a machine's pick and a human's pick should
  -- never be presented as the same claim.
  source         text not null default 'run',
  position       integer not null default 0,
  captured_at    timestamptz not null default now(),
  unique (file_id, start_line, end_line, source),
  constraint hc_excerpt_source_known check (
    source in ('run', 'goal', 'reader')),
  constraint hc_excerpt_lines_sane check (end_line >= start_line),
  -- A passage, not a file. The cap is the policy: anything longer is a
  -- copy of the repository wearing an excerpt's clothes.
  constraint hc_excerpt_bounded check (length(content) <= 20000)
);

create index if not exists hc_excerpts_file on public.hc_file_excerpts (file_id);
create index if not exists hc_excerpts_goal on public.hc_file_excerpts (goal_id);

-- -------------------------------------------------------------------- rls

alter table public.hc_files         enable row level security;
alter table public.hc_runs          enable row level security;
alter table public.hc_run_files     enable row level security;
alter table public.hc_file_excerpts enable row level security;

-- The same two-policy shape as everything else: the owner writes, a member
-- reads. Permissive policies OR together, so reads widen to "mine or shared
-- with me" while writes stay "mine" -- and `using` governs DELETE, which is
-- why widening the FOR ALL policy would have been the wrong move.
do $$
declare t text;
begin
  foreach t in array array['hc_files', 'hc_runs', 'hc_run_files',
                           'hc_file_excerpts']
  loop
    execute format('drop policy if exists %I on public.%I', t || '_owner', t);
    execute format(
      'create policy %I on public.%I for all to authenticated '
      'using (user_id = (select auth.uid())) '
      'with check (user_id = (select auth.uid()))', t || '_owner', t);
    execute format('drop policy if exists %I on public.%I',
                   t || '_member_read', t);
    execute format(
      'create policy %I on public.%I for select to authenticated '
      'using (project_id in (select public.hc_member_projects()))',
      t || '_member_read', t);
  end loop;
end $$;

-- ------------------------------------------------------------------- sync

-- One project's file provenance, in one transaction.
--
-- The prune rule differs per table, and the difference is the point:
--
--   files     never deleted -- a file that left the disk is marked missing,
--             because runs still refer to it and history is not a snapshot.
--   runs      never deleted -- same reason. A run happened.
--   run_files pruned within the runs this payload carries, and only those.
--             The payload is complete for a run or says nothing about it.
--   excerpts  pruned within the files this payload carries. Re-selecting is
--             how an excerpt stops being relevant.
create or replace function public.hc_sync_files(payload jsonb)
returns jsonb
language plpgsql
security invoker
set search_path = public
as $$
declare
  uid uuid := (select auth.uid());
  pid uuid := (payload ->> 'project_id')::uuid;
  runs_touched  uuid[];
  files_touched uuid[];
begin
  if uid is null then
    raise exception 'hc_sync_files: no authenticated user';
  end if;
  if pid is null then
    raise exception 'hc_sync_files: payload has no project_id';
  end if;
  if (payload ->> 'user_id') is distinct from uid::text then
    raise exception 'hc_sync_files: payload is not owned by the caller';
  end if;
  -- The project must already exist. Unlike hc_sync_project this never
  -- creates one: file provenance is about a project, not a way to mint one.
  if not exists (select 1 from public.hc_projects p where p.id = pid) then
    raise exception 'hc_sync_files: no such project';
  end if;
  if not exists (select 1 from public.hc_projects p
                  where p.id = pid and p.user_id = uid)
     and not public.hc_can_write(pid) then
    raise exception 'hc_sync_files: you are not a contributor to this project';
  end if;

  insert into public.hc_files
    (id, user_id, project_id, path, name, ext, size_bytes, modified_at,
     content_sha256, git_tracked, git_status, git_blob_sha, last_commit_sha,
     last_commit_at, edits, missing, binary_file, last_seen_at)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'path',
         coalesce(r ->> 'name', ''), coalesce(r ->> 'ext', ''),
         (r ->> 'size_bytes')::bigint, (r ->> 'modified_at')::timestamptz,
         coalesce(r ->> 'content_sha256', ''),
         coalesce((r ->> 'git_tracked')::boolean, false),
         coalesce(r ->> 'git_status', ''),
         coalesce(r ->> 'git_blob_sha', ''),
         coalesce(r ->> 'last_commit_sha', ''),
         (r ->> 'last_commit_at')::timestamptz,
         coalesce((r ->> 'edits')::int, 0),
         coalesce((r ->> 'missing')::boolean, false),
         coalesce((r ->> 'binary_file')::boolean, false),
         now()
  from jsonb_array_elements(payload -> 'files') r
  on conflict (id) do update set
    path = excluded.path, name = excluded.name, ext = excluded.ext,
    size_bytes = excluded.size_bytes, modified_at = excluded.modified_at,
    content_sha256 = excluded.content_sha256,
    git_tracked = excluded.git_tracked, git_status = excluded.git_status,
    git_blob_sha = excluded.git_blob_sha,
    last_commit_sha = excluded.last_commit_sha,
    last_commit_at = excluded.last_commit_at,
    -- Edits only ever climb: a payload built from the runs it happens to
    -- hold must not walk back a count the remote learned from another one.
    edits = greatest(public.hc_files.edits, excluded.edits),
    missing = excluded.missing, binary_file = excluded.binary_file,
    last_seen_at = now();

  insert into public.hc_runs
    (id, user_id, project_id, session_id, goal_id, status, state, started_at,
     finished_at, cwd, git_branch, git_head_before, git_head_after,
     committed, summary, task_total, files_total, updated_at)
  select (r ->> 'id')::uuid, uid, pid, r ->> 'session_id',
         (r ->> 'goal_id')::uuid, coalesce(r ->> 'status', ''),
         coalesce(r ->> 'state', ''), (r ->> 'started_at')::timestamptz,
         (r ->> 'finished_at')::timestamptz, coalesce(r ->> 'cwd', ''),
         coalesce(r ->> 'git_branch', ''),
         coalesce(r ->> 'git_head_before', ''),
         coalesce(r ->> 'git_head_after', ''),
         coalesce((r ->> 'committed')::boolean, false),
         coalesce(r ->> 'summary', ''),
         coalesce((r ->> 'task_total')::int, 0),
         coalesce((r ->> 'files_total')::int, 0), now()
  from jsonb_array_elements(payload -> 'runs') r
  on conflict (id) do update set
    session_id = excluded.session_id, goal_id = excluded.goal_id,
    status = excluded.status, state = excluded.state,
    started_at = excluded.started_at, finished_at = excluded.finished_at,
    cwd = excluded.cwd, git_branch = excluded.git_branch,
    git_head_before = excluded.git_head_before,
    git_head_after = excluded.git_head_after,
    committed = excluded.committed, summary = excluded.summary,
    task_total = excluded.task_total, files_total = excluded.files_total,
    updated_at = now();

  select coalesce(array_agg((r ->> 'id')::uuid), '{}') into runs_touched
    from jsonb_array_elements(payload -> 'runs') r;
  select coalesce(array_agg((r ->> 'id')::uuid), '{}') into files_touched
    from jsonb_array_elements(payload -> 'files') r;

  insert into public.hc_run_files
    (id, user_id, project_id, run_id, file_id, path, tool, edits,
     lines_added, lines_removed, commit_sha, first_at, last_at)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'run_id')::uuid,
         (r ->> 'file_id')::uuid, coalesce(r ->> 'path', ''),
         coalesce(r ->> 'tool', ''), coalesce((r ->> 'edits')::int, 0),
         (r ->> 'lines_added')::int, (r ->> 'lines_removed')::int,
         coalesce(r ->> 'commit_sha', ''),
         (r ->> 'first_at')::timestamptz, (r ->> 'last_at')::timestamptz
  from jsonb_array_elements(payload -> 'run_files') r
  on conflict (id) do update set
    path = excluded.path, tool = excluded.tool, edits = excluded.edits,
    lines_added = excluded.lines_added,
    lines_removed = excluded.lines_removed,
    commit_sha = excluded.commit_sha, first_at = excluded.first_at,
    last_at = excluded.last_at;

  delete from public.hc_run_files f
   where f.project_id = pid
     and f.run_id = any (runs_touched)
     and f.id not in (select (r ->> 'id')::uuid
                        from jsonb_array_elements(payload -> 'run_files') r);

  insert into public.hc_file_excerpts
    (id, user_id, project_id, file_id, goal_id, run_id, start_line, end_line,
     content, file_sha256, reason, source, position, captured_at)
  select (r ->> 'id')::uuid, uid, pid, (r ->> 'file_id')::uuid,
         (r ->> 'goal_id')::uuid, (r ->> 'run_id')::uuid,
         coalesce((r ->> 'start_line')::int, 1),
         coalesce((r ->> 'end_line')::int, 1),
         coalesce(r ->> 'content', ''), coalesce(r ->> 'file_sha256', ''),
         coalesce(r ->> 'reason', ''), coalesce(r ->> 'source', 'run'),
         coalesce((r ->> 'position')::int, 0), now()
  from jsonb_array_elements(payload -> 'excerpts') r
  on conflict (id) do update set
    goal_id = excluded.goal_id, run_id = excluded.run_id,
    start_line = excluded.start_line, end_line = excluded.end_line,
    content = excluded.content, file_sha256 = excluded.file_sha256,
    reason = excluded.reason, source = excluded.source,
    position = excluded.position, captured_at = now();

  -- A reader's own marking is theirs to remove, not the machine's: the
  -- prune leaves source='reader' rows alone.
  delete from public.hc_file_excerpts e
   where e.project_id = pid
     and e.file_id = any (files_touched)
     and e.source <> 'reader'
     and e.id not in (select (r ->> 'id')::uuid
                        from jsonb_array_elements(payload -> 'excerpts') r);

  update public.hc_projects p set updated_at = now() where p.id = pid;

  return jsonb_build_object(
    'ok', true,
    'files', (select count(*) from public.hc_files where project_id = pid),
    'runs', (select count(*) from public.hc_runs where project_id = pid),
    'run_files', (select count(*) from public.hc_run_files where project_id = pid),
    'excerpts', (select count(*) from public.hc_file_excerpts where project_id = pid));
end $$;

revoke all on function public.hc_sync_files(jsonb) from public;
grant execute on function public.hc_sync_files(jsonb) to authenticated;

-- --------------------------------------------------- the shared reader too

-- A token holder gets the same four tables, owner id stripped, exactly as
-- they get the goals. Rebuilt in full rather than patched: the function is
-- the one door onto a shared project and it should be readable in one go.
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
                  from public.hc_related_prompts x where x.project_id = pid), '[]'::jsonb),
    'files', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_files x where x.project_id = pid), '[]'::jsonb),
    'runs', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_runs x where x.project_id = pid), '[]'::jsonb),
    'run_files', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_run_files x where x.project_id = pid), '[]'::jsonb),
    'excerpts', coalesce((select jsonb_agg(to_jsonb(x) - 'user_id')
                  from public.hc_file_excerpts x where x.project_id = pid), '[]'::jsonb)
  ) into doc;
  return doc;
end $$;

revoke all on function public.hc_read_shared(text) from public;
grant execute on function public.hc_read_shared(text) to anon, authenticated;
