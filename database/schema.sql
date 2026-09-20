-- =============================================================
-- CarbonScope — PostgreSQL / Supabase schema
-- Data Mining-Based Carbon Footprint Estimation and Analysis System
-- =============================================================
-- Covers: activity input (Estimate page), emission factor lookup,
-- K-Means cluster results, Isolation Forest anomaly results,
-- model evaluation metrics, and processing/traceability logging
-- (SRS 4.1.3–4.1.7, 5.1.1, 5.3.1, 5.4.3.1).
-- =============================================================

create extension if not exists "uuid-ossp";

-- -------------------------------------------------------------
-- 1. datasets
-- Documents each existing source dataset used, per the KDD
-- "Data Source Identification and Selection" methodology step.
-- -------------------------------------------------------------
create table datasets (
    dataset_id      uuid primary key default uuid_generate_v4(),
    name            text not null,
    source          text not null,              -- e.g. published paper, public repository
    unit_of_observation text,                    -- what one row represents
    time_period     text,                        -- period the data covers
    variables       text,                        -- short description of included variables
    limitations     text,                        -- documented limitations/assumptions
    is_active       boolean not null default true,
    created_at      timestamptz not null default now()
);

-- -------------------------------------------------------------
-- 2. emission_factors
-- Documented, versioned emission factors used to convert
-- activity quantities into kg CO2e (SRS 4.3.3, methodology
-- step 3 — Data Transformation and Carbon-Footprint Estimation).
-- -------------------------------------------------------------
create table emission_factors (
    factor_id       uuid primary key default uuid_generate_v4(),
    category        text not null check (category in
                        ('transport', 'electricity', 'devices', 'paper', 'food')),
    subtype         text not null,               -- e.g. 'car_gasoline', 'traditional_jeep', 'mixed_diet'
    label           text not null,               -- display label, e.g. 'Car — gasoline'
    factor_value    numeric(10,4) not null check (factor_value >= 0),
    unit            text not null,               -- e.g. 'kg CO2e per km', 'kg CO2e per kWh'
    source          text not null,               -- documented reference for the factor
    is_active       boolean not null default true,
    effective_date  date not null default current_date,
    created_at      timestamptz not null default now(),
    unique (category, subtype, effective_date)
);

-- -------------------------------------------------------------
-- 3. activity_records
-- One row per Estimate submission. Mirrors the Estimate page's
-- input fields; check constraints enforce the same bounds as
-- the sliders (SRS 5.2.1, 5.4.3.1 — validation at form and DB
-- level).
-- -------------------------------------------------------------
create table activity_records (
    record_id           uuid primary key default uuid_generate_v4(),
    dataset_id          uuid references datasets(dataset_id),
    transport_factor_id uuid references emission_factors(factor_id),
    food_factor_id      uuid references emission_factors(factor_id),

    distance_km         numeric(6,2) not null check (distance_km between 0 and 300),
    electricity_kwh     numeric(6,2) not null check (electricity_kwh between 0 and 150),
    device_hours_per_day numeric(4,2) not null check (device_hours_per_day between 0 and 24),
    paper_sheets        numeric(6,2) not null check (paper_sheets between 0 and 100),

    transport_co2e      numeric(8,3) not null check (transport_co2e >= 0),
    electricity_co2e    numeric(8,3) not null check (electricity_co2e >= 0),
    devices_co2e        numeric(8,3) not null check (devices_co2e >= 0),
    paper_co2e          numeric(8,3) not null check (paper_co2e >= 0),
    food_co2e           numeric(8,3) not null check (food_co2e >= 0),
    total_co2e          numeric(8,3) generated always as
                            (transport_co2e + electricity_co2e + devices_co2e + paper_co2e + food_co2e) stored,

    submitted_at        timestamptz not null default now()
);

create index idx_activity_records_submitted_at on activity_records (submitted_at);

-- -------------------------------------------------------------
-- 4. mining_runs
-- One row per K-Means + Isolation Forest execution over the
-- accumulated dataset. Supports the caching requirement (SRS
-- 5.1.1): the Report page reads the latest run instead of
-- recomputing on every page load.
-- -------------------------------------------------------------
create table mining_runs (
    run_id                  uuid primary key default uuid_generate_v4(),
    records_processed       integer not null check (records_processed >= 0),
    k_clusters              integer not null check (k_clusters > 0),
    silhouette_score        numeric(5,4),
    isolation_contamination numeric(5,4),        -- e.g. 0.0060 for 0.6%
    triggered_by            text,                -- 'schedule', 'manual', 'dataset_change'
    started_at              timestamptz not null default now(),
    completed_at            timestamptz,
    status                  text not null default 'completed'
                                check (status in ('running', 'completed', 'failed')),
    notes                   text
);

-- -------------------------------------------------------------
-- 5. cluster_results
-- K-Means output per activity record, tied to the run that
-- produced it (SRS 4.1.5 — cluster distribution on Report page,
-- 4.1.4 — nearest cluster on Estimate results view).
-- -------------------------------------------------------------
create table cluster_results (
    id                  uuid primary key default uuid_generate_v4(),
    run_id              uuid not null references mining_runs(run_id) on delete cascade,
    record_id           uuid not null references activity_records(record_id) on delete cascade,
    cluster_label       text not null check (cluster_label in
                            ('low_emission', 'moderate', 'high_emission')),
    distance_to_centroid numeric(8,4),
    created_at          timestamptz not null default now(),
    unique (run_id, record_id)
);

create index idx_cluster_results_run on cluster_results (run_id);
create index idx_cluster_results_record on cluster_results (record_id);

-- -------------------------------------------------------------
-- 6. anomaly_results
-- Isolation Forest output per activity record (SRS 4.1.6 —
-- flagged records with a plain-language explanation, score,
-- and date).
-- -------------------------------------------------------------
create table anomaly_results (
    id              uuid primary key default uuid_generate_v4(),
    run_id          uuid not null references mining_runs(run_id) on delete cascade,
    record_id       uuid not null references activity_records(record_id) on delete cascade,
    is_anomaly      boolean not null default false,
    anomaly_score   numeric(6,4) not null,        -- Isolation Forest decision_function output
    explanation     text,                          -- plain-language reason for the flag
    created_at      timestamptz not null default now(),
    unique (run_id, record_id)
);

create index idx_anomaly_results_flagged on anomaly_results (is_anomaly) where is_anomaly = true;

-- -------------------------------------------------------------
-- 7. processing_log
-- Traceability log for dataset-processing events (SRS 5.3.1):
-- when preprocessing, clustering, or anomaly detection ran and
-- how many records were involved.
-- -------------------------------------------------------------
create table processing_log (
    log_id          uuid primary key default uuid_generate_v4(),
    event_type      text not null check (event_type in
                        ('preprocessing', 'clustering', 'anomaly_detection', 'export')),
    run_id          uuid references mining_runs(run_id),
    records_processed integer,
    status          text not null check (status in ('success', 'failure')),
    details         jsonb,
    occurred_at     timestamptz not null default now()
);

create index idx_processing_log_occurred_at on processing_log (occurred_at);

-- -------------------------------------------------------------
-- 8. Convenience view for the Report page
-- Pulls totals, cluster distribution, and the latest model
-- evaluation metrics from the most recent completed run.
-- -------------------------------------------------------------
create view latest_run_summary as
select
    r.run_id,
    r.records_processed,
    r.silhouette_score,
    r.isolation_contamination,
    r.completed_at,
    (select round(sum(a.total_co2e) / 1000.0, 2)
        from activity_records a) as total_co2e_tonnes,
    (select count(*) from cluster_results c
        where c.run_id = r.run_id and c.cluster_label = 'low_emission') as low_emission_count,
    (select count(*) from cluster_results c
        where c.run_id = r.run_id and c.cluster_label = 'moderate') as moderate_count,
    (select count(*) from cluster_results c
        where c.run_id = r.run_id and c.cluster_label = 'high_emission') as high_emission_count,
    (select count(*) from anomaly_results an
        where an.run_id = r.run_id and an.is_anomaly = true) as anomaly_count
from mining_runs r
where r.status = 'completed'
order by r.completed_at desc
limit 1;

-- -------------------------------------------------------------
-- 9. Row Level Security (Supabase)
-- Enable RLS and lock write access to the anon key; the Python
-- backend should write through a service-role connection while
-- the frontend reads through scoped policies (SRS 5.3.2).
-- -------------------------------------------------------------
alter table activity_records enable row level security;
alter table cluster_results enable row level security;
alter table anomaly_results enable row level security;
alter table mining_runs enable row level security;
alter table processing_log enable row level security;

create policy "public can read activity records"
    on activity_records for select
    using (true);

create policy "public can read cluster results"
    on cluster_results for select
    using (true);

create policy "public can read anomaly results"
    on anomaly_results for select
    using (true);

create policy "public can read mining runs"
    on mining_runs for select
    using (true);

-- Inserts/updates are intentionally left without a public policy;
-- only the backend's service-role key can write to these tables.
