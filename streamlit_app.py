-- Tambah/ubah unique key jadi hanya (periode_date, rm)
drop index if exists reviews_periode_file_rm_key;
-- kalau dulu sempat bikin unique (periode_date,file_name,rm), hapus constraint itu:
do $$
begin
  if exists (
    select 1 from pg_constraint
    where conrelid = 'public.reviews'::regclass
      and conname = 'reviews_periode_date_file_name_rm_key'
  ) then
    alter table public.reviews drop constraint reviews_periode_date_file_name_rm_key;
  end if;
exception when undefined_object then null;
end$$;

-- pastikan unique baru
alter table public.reviews
  add constraint reviews_periode_date_rm_key unique (periode_date, rm);
