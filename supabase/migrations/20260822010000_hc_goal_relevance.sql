-- How each goal stands to the project's objective.
--
-- Three answers, not two. "supporting" is the one that earns its keep: work
-- that does not serve the objective but unblocks something that does. A
-- binary aligned/unaligned would fold away exactly the plumbing that makes
-- the objective reachable, which is the most expensive thing to lose sight
-- of.
--
-- relevance_for holds the objective the verdict was made against. A verdict
-- outlives the sentence that produced it, and one made against an objective
-- the project no longer states should be visible as stale rather than
-- quietly trusted.
--
-- Default 'core' throughout: nothing is hidden by a column being added, and
-- a project with no objective has nothing for a goal to be unrelated to.

alter table public.hc_goals
  add column if not exists relevance text not null default 'core';
alter table public.hc_goals
  add column if not exists relevance_why text not null default '';
alter table public.hc_goals
  add column if not exists relevance_for text not null default '';

do $$
begin
  if not exists (select 1 from pg_constraint
                  where conname = 'hc_goals_relevance_known') then
    alter table public.hc_goals
      add constraint hc_goals_relevance_known
      check (relevance in ('core', 'supporting', 'unrelated'));
  end if;
end $$;

-- The read a fold will make: everything off the objective, for one project.
create index if not exists hc_goals_off_objective
  on public.hc_goals (project_id) where relevance = 'unrelated';
