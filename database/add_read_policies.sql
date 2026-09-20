-- Run this AFTER schema.sql, once RLS has been enabled on all tables.
-- Adds public read access to the two tables schema.sql didn't cover explicitly.

create policy "public can read datasets"
    on datasets for select
    using (true);

create policy "public can read emission factors"
    on emission_factors for select
    using (true);
