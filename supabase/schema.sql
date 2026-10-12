-- MoreAbundance app: storage in Supabase
-- Run this ONCE: Supabase dashboard → SQL Editor → New query → paste everything → Run.
-- Safe to run again (it only creates what is missing).
--
-- The app keeps each data file (products.csv, sup_codes.json, stock.json) as one row, exactly the
-- same text the files hold today, so every page keeps working as it does now.
-- Every save also keeps the previous version in app_file_history (the last 300 per file),
-- like the commit history of the GitHub data branch.

create table if not exists public.app_files (
  name       text primary key,
  content    text not null,
  message    text not null default '',
  updated_at timestamptz not null default now()
);

create table if not exists public.app_file_history (
  id       bigserial primary key,
  name     text not null,
  content  text not null,
  message  text not null default '',
  saved_at timestamptz not null default now()
);
create index if not exists app_file_history_name_id on public.app_file_history (name, id desc);

-- Keep the old version on every change, trim to the last 300 versions per file.
create or replace function public.app_files_keep_history() returns trigger
language plpgsql as $$
begin
  if new.content is distinct from old.content then
    insert into public.app_file_history (name, content, message, saved_at)
    values (old.name, old.content, old.message, old.updated_at);
    delete from public.app_file_history
     where name = old.name
       and id not in (select id from public.app_file_history
                       where name = old.name order by id desc limit 300);
  end if;
  new.updated_at := now();
  return new;
end $$;

create or replace trigger app_files_history before update on public.app_files
  for each row execute function public.app_files_keep_history();

-- Only the app's secret key may read or write. Row Level Security with no policies blocks the
-- public (anon / publishable) key completely; the secret key bypasses it.
alter table public.app_files enable row level security;
alter table public.app_file_history enable row level security;

-- The app talks to these tables through the Data API with the secret (service_role) key.
grant usage on schema public to service_role;
grant select, insert, update, delete on public.app_files, public.app_file_history to service_role;
grant usage, select on sequence public.app_file_history_id_seq to service_role;

-- Marker row: the app checks it to know this script has been run and the key can read the table.
insert into public.app_files (name, content, message)
values ('_ready', 'ok', 'schema.sql')
on conflict (name) do nothing;
