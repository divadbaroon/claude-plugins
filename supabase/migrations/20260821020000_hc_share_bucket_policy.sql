-- Letting the workspace publish its own viewer page.
--
-- The bucket is public to READ -- that is the point; a collaborator opens
-- the page with no account. Writing is a different question, and by default
-- storage.objects has no policy at all, so nobody but the service key can
-- upload. This grants writing on exactly one bucket, to signed-in users of
-- this project -- which, on a single-person project, is the owner alone.
--
-- Scoped to bucket_id: it says nothing about any other bucket, so a later
-- bucket holding something private is unaffected.

drop policy if exists "hc_share_bucket_write" on storage.objects;
create policy "hc_share_bucket_write" on storage.objects
  for all to authenticated
  using (bucket_id = 'hc-share')
  with check (bucket_id = 'hc-share');
