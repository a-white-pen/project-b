# Data Dictionary
_Auto-generated. Do not edit by hand. Run `python schema/dump_data_dictionary.py` to refresh._

## Schema: `b`

### Table: `b.aligner_tray_changes`
One row per Invisalign tray activation per arch. Current tray for an arch is its open row (ended_at NULL); a new tray auto-closes the prior one. Treatment start = earliest started_at.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `aligner_tray_change_id` | `integer` | no | nextval('b.aligner_tray_changes_aligner_tray_change_id_seq'::regclass) | Surrogate primary key. |
| `arch` | `text` | no |  | Which arch this tray belongs to: upper or lower. Arches advance independently. |
| `tray_number` | `integer` | no |  | The tray number B switched to for this arch. |
| `planned_days` | `integer` | yes |  | Dentist's planned wear duration for this tray, in days. Optional. |
| `started_at` | `timestamp with time zone` | no |  | When B switched to this tray. Primary time dimension. |
| `ended_at` | `timestamp with time zone` | yes |  | When B switched off this tray, auto-set to the next tray's started_at for the same arch. NULL means this is the current tray. |
| `notes` | `text` | yes |  | Optional detail or correction note. |
| `meta` | `jsonb` | no | '{}'::jsonb | Provenance + lifecycle: corrections[]; spawned rows carry start.wear_event_id (source event, for reconcile/cascade) + start.reason=spawned_from_wear_correction; started_at_pinned=true once B directly corrects the start (suppresses auto-retime). |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation timestamp; NULL for rows never changed after insert. |

### Table: `b.aligner_wear_events`
One row per out-of-mouth event for B's Invisalign aligners (one removal/reinsertion cycle). Open row (reinserted_at NULL) means currently out; out-time per day = sum of events, worn = 24h minus out.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `aligner_wear_event_id` | `integer` | no | nextval('b.aligner_wear_events_aligner_wear_event_id_seq'::regclass) | Surrogate primary key. |
| `removed_at` | `timestamp with time zone` | no |  | When the aligners came out of the mouth. Primary time dimension for out-time analysis. |
| `reinserted_at` | `timestamp with time zone` | yes |  | When the aligners were put back in. NULL means this is the currently open event (aligners out right now). |
| `upper_tray_number` | `integer` | yes |  | Upper-arch tray in use AS-OF removed_at. DERIVED CACHE recomputed from b.aligner_tray_changes (never edited independently); NULL if no tray had started by then. |
| `lower_tray_number` | `integer` | yes |  | Lower-arch tray in use AS-OF removed_at. DERIVED CACHE recomputed from b.aligner_tray_changes (never edited independently); NULL if no tray had started by then. |
| `notes` | `text` | yes |  | Optional extra detail or correction note. NULL if nothing useful to add. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source provenance and lifecycle metadata: start/end source, self_reported, telegram_update_id, and a corrections array. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Use removed_at/reinserted_at for analysis. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation timestamp, set when an event is closed or corrected. NULL for rows never changed after insert. |

### Table: `b.attention_sessions`
One row per continuous primary-attention interval for B. Grain: one activity session with a start time and optional end time.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `attention_session_id` | `integer` | no | nextval('b.attention_sessions_attention_session_id_seq'::regclass) | Surrogate primary key. |
| `category` | `text` | no |  | Top-level activity category. Values: work, social, self_care, eat, downtime, admin, transit, other. Combined with subcategory to form a valid pair — see attention_sessions_taxonomy_check. |
| `description` | `text` | no |  | Human-readable description of what B was doing, preserving B's wording where useful. Examples: working on attention module, prep breakfast, watching Succession. |
| `project` | `text` | yes |  | Optional project or context tag, e.g. project-b, work, Codex, a book title, or a show title. |
| `started_at` | `timestamp with time zone` | no |  | When the attention session started. Primary start time dimension for attention analysis. For Telegram rows, usually the Telegram message timestamp. |
| `ended_at` | `timestamp with time zone` | yes |  | When the attention session ended. NULL means this is the currently open session. Session duration is derived from started_at and ended_at in marts/views. |
| `notes` | `text` | yes |  | Optional extra detail, outcome, or correction note. NULL if there is nothing useful to add. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source provenance, lifecycle details, and classifier metadata. Expected shape: {"start":{"source":"telegram","self_reported":true,"telegram_update_id":123},"end":{"source":"telegram\|system\|calendar\|reminder","self_reported":true\|false,"reason":"explicit_finish\|superseded_by_new_start\|manual_correction\|source_reported","telegram_update_id":124},"classification":{"model":"gemini-2.5-flash","action":"start_session"}}. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Use started_at and ended_at for time-series and duration analysis. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation timestamp, set by application code when a session is ended or corrected. NULL for rows that have not been changed after insert. |
| `subcategory` | `text` | no |  | Specific activity within category. Valid (category, subcategory) pairs: work / {deep_work, shallow_work, meetings, learning, planning}; social / {social_in_person, social_messaging, social_broadcast}; self_care / {exercise, personal_care, meditation}; eat / {food_prep, food_collection, eating}; downtime / {rest, entertainment}; admin / {shopping_online, shopping_in_store, errands, life_admin, health_admin}; transit / {commute, travel}; other / {other}. |

### View: `b.latest_location`
Most recent location B has shared. Used by domain services to get the active timezone for local time-of-day inference. Application code falls back to Asia/Singapore if no rows exist.

**View definition:**
```sql
SELECT location_id,
    telegram_update_id,
    latitude,
    longitude,
    timezone,
    location_name,
    created_at
   FROM b.location
  ORDER BY created_at DESC
 LIMIT 1;
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `location_id` | `integer` | yes |  |  |
| `telegram_update_id` | `bigint` | yes |  |  |
| `latitude` | `numeric(9,6)` | yes |  |  |
| `longitude` | `numeric(9,6)` | yes |  |  |
| `timezone` | `text` | yes |  |  |
| `location_name` | `text` | yes |  |  |
| `created_at` | `timestamp with time zone` | yes |  |  |

### Table: `b.location`
Log of every location B shares via Telegram. One row per LOCATION message. timezone is derived at insert time from lat/lon using timezonefinder (Python library, offline, no API) and is immutable after insert. location_name and country are backfilled in a single UPDATE after the row is committed — Nominatim (OpenStreetMap, free, no API key) is called best-effort and may leave them NULL on geocoding failure; no other columns are ever changed. Application code falls back to Asia/Singapore if this table has no rows. Use b.latest_location view to get the active timezone.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `location_id` | `integer` | no | nextval('b.location_location_id_seq'::regclass) | Surrogate primary key. |
| `telegram_update_id` | `bigint` | yes |  | update_id of the inbound location update. Joins to system.telegram_inbound.update_id. |
| `latitude` | `numeric(9,6)` | no |  | WGS-84 latitude from the Telegram location message. |
| `longitude` | `numeric(9,6)` | no |  | WGS-84 longitude from the Telegram location message. |
| `timezone` | `text` | no |  | IANA timezone string derived offline via timezonefinder. e.g. Asia/Bangkok, Asia/Singapore. |
| `location_name` | `text` | yes |  | Human-readable district and city in English via Nominatim. e.g. Bang Sue, Bangkok. Null if geocoding fails. |
| `created_at` | `timestamp with time zone` | no | now() | Timestamp of the Telegram location message. |
| `country` | `text` | yes |  | Country (English) from Nominatim reverse geocoding at insert time. NULL for rows geocoded before this column existed or on geocoding failure. |

### Table: `b.period_days`
Sparse: one row per day B is on her period (presence = period). Manual logging now; future Oura (temperature-inferred) or Garmin Women's Health. Predictions are computed on-demand from history (no stored predicted column). A dense every-day true/false readout is a VIEW over generate_series, not stored. Use: flag water-retention weeks for the weight-trend calibration.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `period_day_id` | `integer` | no | nextval('b.period_days_period_day_id_seq'::regclass) |  |
| `period_date` | `date` | no |  | A period day. UNIQUE. |
| `source` | `text` | no | 'manual'::text | manual \| oura \| garmin \| apple. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source-specific extras. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |

### Table: `b.sleep_wake_events`
One row per sleep boundary event for B. Grain: one sleep or one wake event. Pair a sleep row and a wake row to derive session duration. No telegram_update_id column — Telegram provenance is stored in meta. Deduplication is handled upstream by the webhook (system.telegram_inbound unique on update_id).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `sleep_wake_event_id` | `integer` | no | nextval('b.sleep_wake_events_sleep_wake_event_id_seq'::regclass) |  |
| `event_type` | `text` | no |  | Boundary type. Values: sleep (went to sleep), wake (woke up). |
| `occurred_at` | `timestamp with time zone` | no |  | When the event happened. For Telegram rows: set to msg.timestamp. For device imports (Garmin, Oura, Whoop): set to the actual event timestamp from the device. Primary time dimension for all sleep analysis — never substitute created_at. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source provenance and quality flags. telegram_update_id lives here, not as a column. Telegram: {"source":"telegram","self_reported":true,"telegram_update_id":N}. Garmin: {"source":"garmin","self_reported":false,"session_id":"abc123"}. Oura: {"source":"oura","self_reported":false,"ring_id":"xyz"}. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Use occurred_at for all time-series and duration queries. |

### Table: `b.weight_measurements`
One row per body-weight reading for B. Grain: one measurement. No telegram_update_id column — Telegram provenance is stored in meta. Deduplication is handled upstream by the webhook (system.telegram_inbound unique on update_id).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `weight_measurement_id` | `integer` | no | nextval('b.weight_measurements_weight_measurement_id_seq'::regclass) |  |
| `measured_at` | `timestamp with time zone` | no |  | When the weight measurement happened. For Telegram rows: set to msg.timestamp. For device imports (Withings, Garmin Index): set to the actual reading timestamp from the device. Never substitute created_at. |
| `weight_kg` | `numeric(5,2)` | no |  | Body weight in kilograms. Convert all other units before insert: lbs ÷ 2.20462, stones × 6.35029. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source provenance. telegram_update_id lives here, not as a column. Telegram: {"source":"telegram","self_reported":true,"telegram_update_id":N}. Withings: {"source":"withings","self_reported":false,"device":"Withings Body+"}. Garmin: {"source":"garmin","self_reported":false,"device":"Garmin Index S2"}. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Use measured_at for all time-series queries. |

## Schema: `data_visualisation`

### View: `data_visualisation.aligner_visualisation`
Aligner widget feed (body). wear_events + tray_changes via record_type, rows >=15 min old. Notes excluded.

**View definition:**
```sql
SELECT 'wear_event'::text AS record_type,
    w.aligner_wear_event_id AS id,
    w.removed_at,
    w.reinserted_at,
    w.upper_tray_number,
    w.lower_tray_number,
    NULL::text AS arch,
    NULL::integer AS tray_number,
    NULL::integer AS planned_days,
    NULL::timestamp with time zone AS started_at,
    NULL::timestamp with time zone AS ended_at
   FROM b.aligner_wear_events w
  WHERE w.created_at <= (now() - '00:15:00'::interval)
UNION ALL
 SELECT 'tray_change'::text AS record_type,
    t.aligner_tray_change_id AS id,
    NULL::timestamp with time zone AS removed_at,
    NULL::timestamp with time zone AS reinserted_at,
    NULL::integer AS upper_tray_number,
    NULL::integer AS lower_tray_number,
    t.arch,
    t.tray_number,
    t.planned_days,
    t.started_at,
    t.ended_at
   FROM b.aligner_tray_changes t
  WHERE t.created_at <= (now() - '00:15:00'::interval);
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `record_type` | `text` | yes |  |  |
| `id` | `integer` | yes |  |  |
| `removed_at` | `timestamp with time zone` | yes |  |  |
| `reinserted_at` | `timestamp with time zone` | yes |  |  |
| `upper_tray_number` | `integer` | yes |  |  |
| `lower_tray_number` | `integer` | yes |  |  |
| `arch` | `text` | yes |  |  |
| `tray_number` | `integer` | yes |  |  |
| `planned_days` | `integer` | yes |  |  |
| `started_at` | `timestamp with time zone` | yes |  |  |
| `ended_at` | `timestamp with time zone` | yes |  |  |

### View: `data_visualisation.location_visualisation`
Awake & location widget feed (today). City (from location_name, district dropped) + country (b.location.country, from Nominatim) + timezone. No coordinates.

**View definition:**
```sql
SELECT NULLIF(btrim(regexp_replace(location_name, '^.*,'::text, ''::text)), ''::text) AS city,
    country,
    timezone
   FROM b.location
  WHERE created_at <= (now() - '00:15:00'::interval)
  ORDER BY created_at DESC
 LIMIT 1;
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `city` | `text` | yes |  |  |
| `country` | `text` | yes |  |  |
| `timezone` | `text` | yes |  |  |

### View: `data_visualisation.nutrition_visualisation`
Fuel widget feed. Live view over nutrition.food_log, full history, rows >=15 min old.

**View definition:**
```sql
SELECT food_log_id,
    meal_type,
    food_item,
    kcal,
    protein_g,
    carbs_g,
    fat_g,
    fibre_g,
    sugar_g,
    sodium_mg,
    created_at AS logged_at,
    now() AS refreshed_at
   FROM nutrition.food_log f
  WHERE created_at <= (now() - '00:15:00'::interval);
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `food_log_id` | `integer` | yes |  |  |
| `meal_type` | `text` | yes |  |  |
| `food_item` | `text` | yes |  |  |
| `kcal` | `numeric(7,2)` | yes |  |  |
| `protein_g` | `numeric(6,2)` | yes |  |  |
| `carbs_g` | `numeric(6,2)` | yes |  |  |
| `fat_g` | `numeric(6,2)` | yes |  |  |
| `fibre_g` | `numeric(6,2)` | yes |  |  |
| `sugar_g` | `numeric(6,2)` | yes |  |  |
| `sodium_mg` | `numeric(7,2)` | yes |  |  |
| `logged_at` | `timestamp with time zone` | yes |  |  |
| `refreshed_at` | `timestamp with time zone` | yes |  |  |

### View: `data_visualisation.sleep_visualisation`
Awake/asleep state for the today widget. Live view over b.sleep_wake_events, full history, rows >=15 min old.

**View definition:**
```sql
SELECT event_type,
    occurred_at
   FROM b.sleep_wake_events
  WHERE created_at <= (now() - '00:15:00'::interval);
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `event_type` | `text` | yes |  |  |
| `occurred_at` | `timestamp with time zone` | yes |  |  |

### View: `data_visualisation.spend_visualisation`
Expenses widget feed (resources). Real spends, SGD>0, last ~6 months, rows >=15 min old. English item names only.

**View definition:**
```sql
SELECT spend_entry_id,
    spent_at,
    merchant_name_raw,
    platform,
    category,
    COALESCE(( SELECT array_agg(t.elem ->> 'name'::text ORDER BY t.ord) AS array_agg
           FROM jsonb_array_elements(
                CASE
                    WHEN jsonb_typeof(s.items_json -> 'lines'::text) = 'array'::text THEN s.items_json -> 'lines'::text
                    ELSE '[]'::jsonb
                END) WITH ORDINALITY t(elem, ord)
          WHERE NULLIF(btrim(t.elem ->> 'name'::text), ''::text) IS NOT NULL), ARRAY[]::text[]) AS items,
    sgd_amount,
    fx_rate_source,
    payment_method
   FROM finances.spend_entries s
  WHERE COALESCE(ignored_reason, ''::text) = ''::text AND sgd_amount > 0::numeric AND spent_at >= (now() - '6 mons'::interval) AND created_at <= (now() - '00:15:00'::interval);
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `spend_entry_id` | `integer` | yes |  |  |
| `spent_at` | `timestamp with time zone` | yes |  |  |
| `merchant_name_raw` | `text` | yes |  |  |
| `platform` | `text` | yes |  |  |
| `category` | `text` | yes |  |  |
| `items` | `text[]` | yes |  |  |
| `sgd_amount` | `numeric(6,2)` | yes |  |  |
| `fx_rate_source` | `text` | yes |  |  |
| `payment_method` | `text` | yes |  |  |

### View: `data_visualisation.weight_visualisation`
Weight widget feed (body). One row per local day (first weigh-in), rows >=15 min old.

**View definition:**
```sql
WITH eligible AS (
         SELECT w.measured_at,
            w.weight_kg
           FROM b.weight_measurements w
          WHERE w.created_at <= (now() - '00:15:00'::interval)
        ), localized AS (
         SELECT e.measured_at,
            e.weight_kg,
            COALESCE(( SELECT l.timezone
                   FROM b.location l
                  WHERE l.created_at <= e.measured_at
                  ORDER BY l.created_at DESC
                 LIMIT 1), ( SELECT latest_location.timezone
                   FROM b.latest_location), 'Asia/Singapore'::text) AS tz
           FROM eligible e
        ), with_wake AS (
         SELECT lo.measured_at,
            lo.weight_kg,
            (lo.measured_at AT TIME ZONE lo.tz) AS measured_at_local,
            wake.occurred_at AS wake_at
           FROM localized lo
             LEFT JOIN LATERAL ( SELECT e.occurred_at
                   FROM b.sleep_wake_events e
                  WHERE e.event_type = 'wake'::text AND e.occurred_at <= lo.measured_at AND e.occurred_at >= (lo.measured_at - '06:00:00'::interval) AND (( SELECT s.event_type
                           FROM b.sleep_wake_events s
                          WHERE s.occurred_at < e.occurred_at
                          ORDER BY s.occurred_at DESC
                         LIMIT 1)) = 'sleep'::text
                  ORDER BY e.occurred_at DESC
                 LIMIT 1) wake ON true
        )
 SELECT DISTINCT ON ((measured_at_local::date)) measured_at,
    measured_at_local,
    weight_kg,
    round(EXTRACT(epoch FROM measured_at - wake_at) / 60::numeric)::integer AS minutes_after_wake
   FROM with_wake
  ORDER BY (measured_at_local::date), measured_at;
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `measured_at` | `timestamp with time zone` | yes |  |  |
| `measured_at_local` | `timestamp without time zone` | yes |  |  |
| `weight_kg` | `numeric(5,2)` | yes |  |  |
| `minutes_after_wake` | `integer` | yes |  |  |

## Schema: `exercise`

### View: `exercise.activities`
Unified read model at SESSION grain across cardio_activities, strength_sessions, and other_exercises. Columns: kind (cardio/strength/movement) and category (run/walk/ride/swim for cardio; strength for strength; yoga/pilates/climbing/etc. for movement) give two filter levels — agent queries either bucket. Common header columns are present on every row; kind-specific extras (distance_m, total_active_sets, etc.) live in the details JSONB. Drill into per-km splits via source_table=cardio_activities + source_id → exercise.cardio_splits; per-set detail via source_table=strength_sessions + source_id → exercise.strength_sets. Built for agentic consumption — narrow shape minimises NULL noise. For dashboards/ad-hoc SQL, consider querying source tables directly.

**View definition:**
```sql
SELECT 'cardio'::text AS kind,
    cardio_activities.activity_category AS category,
    cardio_activities.started_at,
    cardio_activities.timezone,
    cardio_activities.duration_seconds,
    cardio_activities.average_heartrate AS avg_hr,
    cardio_activities.max_heartrate AS max_hr,
    cardio_activities.calories_kcal,
    cardio_activities.perceived_exertion,
    cardio_activities.device_name,
    'strava'::text AS source_app,
    cardio_activities.strava_activity_id::text AS source_reference,
    jsonb_strip_nulls(jsonb_build_object('activity_name', cardio_activities.activity_name, 'sport_type', cardio_activities.sport_type, 'is_treadmill', cardio_activities.is_treadmill, 'moving_seconds', cardio_activities.moving_seconds, 'distance_m', cardio_activities.distance_m, 'elevation_gain_m', cardio_activities.elevation_gain_m, 'average_speed_mps', cardio_activities.average_speed_mps, 'max_speed_mps', cardio_activities.max_speed_mps, 'average_cadence', cardio_activities.average_cadence, 'gear_name', cardio_activities.gear_name)) AS details,
    'cardio_activities'::text AS source_table,
    cardio_activities.cardio_activity_id AS source_id,
    cardio_activities.created_at,
    cardio_activities.updated_at
   FROM exercise.cardio_activities
UNION ALL
 SELECT 'strength'::text AS kind,
    'strength'::text AS category,
    strength_sessions.started_at,
    NULL::text AS timezone,
    strength_sessions.duration_seconds,
    strength_sessions.avg_hr,
    strength_sessions.max_hr,
    strength_sessions.calories_kcal,
    strength_sessions.perceived_exertion,
    strength_sessions.device_name,
    strength_sessions.source_app,
    COALESCE(strength_sessions.strava_activity_id::text, strength_sessions.source_activity_id) AS source_reference,
    jsonb_strip_nulls(jsonb_build_object('activity_name', strength_sessions.activity_name, 'total_active_sets', strength_sessions.total_active_sets, 'total_exercises', strength_sessions.total_exercises)) AS details,
    'strength_sessions'::text AS source_table,
    strength_sessions.strength_session_id AS source_id,
    strength_sessions.created_at,
    strength_sessions.updated_at
   FROM exercise.strength_sessions
UNION ALL
 SELECT 'movement'::text AS kind,
    other_exercises.activity_type AS category,
    other_exercises.started_at,
    other_exercises.timezone,
    other_exercises.duration_seconds,
    other_exercises.avg_hr,
    other_exercises.max_hr,
    other_exercises.calories_kcal,
    other_exercises.perceived_exertion,
    other_exercises.device_name,
    other_exercises.source_app,
    COALESCE(other_exercises.strava_activity_id::text, other_exercises.source_activity_id) AS source_reference,
    jsonb_strip_nulls(jsonb_build_object('activity_name', other_exercises.activity_name)) AS details,
    'other_exercises'::text AS source_table,
    other_exercises.other_exercise_id AS source_id,
    other_exercises.created_at,
    other_exercises.updated_at
   FROM exercise.other_exercises;
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `kind` | `text` | yes |  |  |
| `category` | `text` | yes |  |  |
| `started_at` | `timestamp with time zone` | yes |  |  |
| `timezone` | `text` | yes |  |  |
| `duration_seconds` | `integer` | yes |  |  |
| `avg_hr` | `numeric(5,1)` | yes |  |  |
| `max_hr` | `numeric(5,1)` | yes |  |  |
| `calories_kcal` | `integer` | yes |  |  |
| `perceived_exertion` | `integer` | yes |  |  |
| `device_name` | `text` | yes |  |  |
| `source_app` | `text` | yes |  |  |
| `source_reference` | `text` | yes |  |  |
| `details` | `jsonb` | yes |  |  |
| `source_table` | `text` | yes |  |  |
| `source_id` | `integer` | yes |  |  |
| `created_at` | `timestamp with time zone` | yes |  |  |
| `updated_at` | `timestamp with time zone` | yes |  |  |

### Table: `exercise.cardio_activities`
One row per completed cardio activity synced from Strava. Covers runs, walks, hikes, rides, and swims. Non-distance activities (yoga, pilates, climbing, etc.) live in exercise.other_exercises; strength sessions live in exercise.strength_sessions. Grain: one activity. Source payload is in system.strava_inbound.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `cardio_activity_id` | `integer` | no | nextval('exercise.cardio_activities_cardio_activity_id_seq'::regclass) |  |
| `strava_inbound_id` | `integer` | no |  | FK to system.strava_inbound row that triggered this activity save. |
| `strava_activity_id` | `bigint` | no |  | Strava activity ID. Unique — update events overwrite via application upsert logic, not new rows. |
| `activity_name` | `text` | no |  |  |
| `sport_type` | `text` | no |  | Raw Strava sport_type string, e.g. Run, Walk, Ride, TrailRun, VirtualRun, Hike. |
| `activity_category` | `text` | no |  | Normalised Project B category. Values for rows written on/after 2026-05-26: run, walk, ride, swim. Legacy value: other_cardio (historical rows from before non-distance activities were routed to exercise.other_exercises — never produced by the current classifier). |
| `is_treadmill` | `boolean` | no | false | True when Strava trainer=true, indicating a treadmill or indoor trainer session. No GPS data. |
| `started_at` | `timestamp with time zone` | no |  |  |
| `timezone` | `text` | no |  |  |
| `duration_seconds` | `integer` | no |  | Total elapsed time in seconds including pauses. From Strava elapsed_time. |
| `moving_seconds` | `integer` | no |  | Active moving time in seconds excluding stops. From Strava moving_time. Equals duration_seconds for treadmill runs. |
| `distance_m` | `numeric(10,2)` | yes |  | Total distance in metres. Null for activities with no distance tracking. |
| `elevation_gain_m` | `numeric(8,2)` | yes |  | Total elevation gain in metres. Zero for treadmill. From Strava total_elevation_gain. |
| `elev_high_m` | `numeric(8,2)` | yes |  | Highest elevation point in metres. Null for treadmill. |
| `elev_low_m` | `numeric(8,2)` | yes |  | Lowest elevation point in metres. Null for treadmill. |
| `average_speed_mps` | `numeric(6,3)` | yes |  | Average speed in metres per second. Divide into 1000 for pace in sec/km. |
| `max_speed_mps` | `numeric(6,3)` | yes |  |  |
| `average_cadence` | `numeric(5,1)` | yes |  | Average step cadence in steps per minute (one-foot count from Garmin). |
| `average_heartrate` | `numeric(5,1)` | yes |  | Average heart rate in bpm for the full activity. |
| `max_heartrate` | `numeric(5,1)` | yes |  | Peak heart rate in bpm recorded during the activity. |
| `calories_kcal` | `integer` | yes |  | Estimated calories burned. From Strava calories field. |
| `perceived_exertion` | `integer` | yes |  | 1–10 RPE scale. Populated when B sets it in Strava. Null otherwise. |
| `gear_name` | `text` | yes |  | Gear name from Strava, e.g. ASICS Nimbus 27. Useful for shoe mileage tracking. |
| `device_name` | `text` | yes |  |  |
| `polyline` | `text` | yes |  | Google-encoded polyline of the GPS route. Null for treadmill and indoor activities. Decode with any polyline library for map visualisation. |
| `start_lat` | `numeric(9,6)` | yes |  | Latitude of activity start point. Null for treadmill. |
| `start_lng` | `numeric(9,6)` | yes |  | Longitude of activity start point. Null for treadmill. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source provenance and fields not promoted to columns. Shape: {"strava_workout_type": ..., "splits_metric": [...], "external_id": "garmin_ping_..."}. |
| `created_at` | `timestamp with time zone` | no | now() |  |
| `updated_at` | `timestamp with time zone` | yes |  |  |

### Table: `exercise.cardio_plan`
Planned structured run (one row/day; UNIQUE plan_date). Holds detail only for actual runs (treadmill/outdoor); ad-hoc cardio (hash/hike/cycle/swim) sits as activity_type=cardio with NO row here and reconciles loosely against cardio_activities. Real FK to daily_plan + trigger requires activity_type contains 'cardio'.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `cardio_plan_id` | `integer` | no | nextval('exercise.cardio_plan_cardio_plan_id_seq'::regclass) |  |
| `plan_date` | `date` | no |  |  |
| `status` | `text` | no | 'planned'::text | planned -> done \| skipped \| unplanned. Forward-only. |
| `run_type` | `text` | yes |  | easy \| long \| quality \| fartlek. Drives delivery (text vs Garmin push). |
| `run_surface` | `text` | yes |  | treadmill (default) \| outdoor. |
| `target_distance_m` | `integer` | yes |  | Coarse target distance (m). Duration is derived (distance ÷ pace). |
| `target_pace_s_per_km` | `integer` | yes |  | Target pace (seconds per km). |
| `plan` | `jsonb` | yes |  | Day-of detail: text line for easy/long; interval steps for quality (Garmin push). NULL at scaffold. |
| `garmin_workout_id` | `text` | yes |  | Garmin id of the pushed workout (quality/fartlek). NULL otherwise. |
| `completed_cardio_activity_id` | `integer` | yes |  | Loose FK to exercise.cardio_activities, set by the reconciler. |
| `meta` | `jsonb` | no | '{}'::jsonb | Provenance. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation. |

### Table: `exercise.cardio_splits`
Per-km lap data for each cardio activity. One row per Garmin auto-lap (typically 1 km). Combines fields from Strava laps (cadence, max HR, elevation gain) and splits_metric (moving time, elevation difference, grade-adjusted speed). Used for per-km training analysis and AI planning.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `cardio_split_id` | `integer` | no | nextval('exercise.cardio_splits_cardio_split_id_seq'::regclass) |  |
| `cardio_activity_id` | `integer` | no |  |  |
| `lap_index` | `integer` | no |  | Lap number within the activity, 1-based. From Strava laps.lap_index. |
| `distance_m` | `numeric(8,2)` | no |  | Distance covered in this lap in metres. Typically ~1000 m for auto-lap. |
| `elapsed_seconds` | `integer` | no |  | Total time for this lap including pauses. From Strava laps.elapsed_time. |
| `moving_seconds` | `integer` | yes |  | Active moving time for this lap. From splits_metric.moving_time. Equals elapsed_seconds for treadmill. |
| `average_speed_mps` | `numeric(6,3)` | yes |  |  |
| `max_speed_mps` | `numeric(6,3)` | yes |  |  |
| `average_cadence` | `numeric(5,1)` | yes |  | Average step cadence in spm for this lap. From Strava laps. Null if not recorded. |
| `average_heartrate` | `numeric(5,1)` | yes |  | Average heart rate in bpm for this lap. |
| `max_heartrate` | `numeric(5,1)` | yes |  | Peak heart rate in bpm within this lap. From Strava laps. |
| `elevation_gain_m` | `numeric(7,2)` | yes |  | Total elevation gain in metres for this lap. From Strava laps.total_elevation_gain. |
| `elevation_difference_m` | `numeric(7,2)` | yes |  | Net elevation change (gain minus loss) for this lap. From splits_metric.elevation_difference. |
| `grade_adjusted_speed_mps` | `numeric(6,3)` | yes |  | Speed adjusted for gradient — normalises uphill effort to flat equivalent. From splits_metric.average_grade_adjusted_speed. |
| `pace_zone` | `integer` | yes |  | Strava pace zone (1–5) for this lap. Null for walks and some outdoor activities. |
| `created_at` | `timestamp with time zone` | no | now() |  |

### Table: `exercise.other_exercises`
One row per completed activity that is neither distance-based cardio (run/walk/ride/swim) nor strength training. Catches yoga, pilates, rock climbing, and any future Strava sport_type that does not match an explicit cardio or strength bucket. Source-agnostic: source_app + source_activity_id identify the origin platform; same shape as exercise.strength_sessions for cross-source ingestion. No splits or sets sub-table — grain is one session, with type-specific fields in meta.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `other_exercise_id` | `integer` | no | nextval('exercise.other_exercises_other_exercise_id_seq'::regclass) | Surrogate primary key. |
| `strava_inbound_id` | `integer` | yes |  | Loose FK to system.strava_inbound row that triggered this save. NULL when the row did not originate from a Strava webhook (e.g. manual entry or future direct-source ingestion). |
| `strava_activity_id` | `bigint` | yes |  | Strava activity ID. NULL when not Strava-triggered. Unique when present — update events overwrite via application upsert logic, not new rows. |
| `source_app` | `text` | no | 'strava'::text | Platform that recorded the session. strava = Strava webhook (today). Future: garmin (direct Garmin Connect ingest), apple_health, manual. |
| `inbound_row_id` | `integer` | yes |  | Loose FK into the relevant raw inbound table — strava_inbound_id when source_app=strava. No PG foreign key because the target table varies by source. |
| `source_activity_id` | `text` | yes |  | Activity ID in the source platform. TEXT to accommodate non-integer IDs from future sources. |
| `activity_type` | `text` | no |  | Normalised activity type. Examples: yoga, pilates, climbing. Free-form text validated in app code only (no CHECK constraint) so new types can be added without a migration. |
| `activity_name` | `text` | yes |  | Session name as recorded in the source platform, e.g. "Lunchtime Vinyasa" or the raw Strava activity name. |
| `started_at` | `timestamp with time zone` | no |  | UTC timestamp when the session began. |
| `timezone` | `text` | yes |  | IANA timezone string at the time of the activity, e.g. Asia/Bangkok. From the source platform when available. |
| `duration_seconds` | `integer` | yes |  | Total elapsed session duration in seconds, as reported by the source platform. |
| `avg_hr` | `numeric(5,1)` | yes |  | Average heart rate across the full session, in bpm, from the source platform summary. |
| `max_hr` | `numeric(5,1)` | yes |  | Peak heart rate recorded during the session, in bpm. |
| `calories_kcal` | `integer` | yes |  | Active calories burned as reported by the source platform. NULL when not available. |
| `perceived_exertion` | `integer` | yes |  | RPE 1–10, B-reported via Telegram. NULL until reported. Not device-recorded. |
| `device_name` | `text` | yes |  | Name of the recording device, e.g. Garmin Forerunner 265. NULL when not available from source. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source-specific and type-specific fields not promoted to dedicated columns. Examples — climbing: {"hardest_grade": "V4", "route_count": 8}; yoga: {"style": "vinyasa", "instructor": "..."}; strava provenance: {"external_id": "garmin_ping_..."}. Keeps schema stable as new activity types arrive. |
| `created_at` | `timestamp with time zone` | no | now() | Row creation timestamp (UTC). |
| `updated_at` | `timestamp with time zone` | yes |  | Last update timestamp (UTC). Set on any correction or re-sync. |

### Table: `exercise.strength_plan`
Planned strength session (one row/day; UNIQUE plan_date). Sun scaffold writes intent (status planned, plan NULL); the day-of planner REGENERATES the full plan jsonb from all current data and UPSERTs the same row; the reconciler stamps completion. Real FK to daily_plan (CASCADE) + trigger: a row may only exist when daily_plan.activity_type contains 'strength'.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `strength_plan_id` | `integer` | no | nextval('exercise.strength_plan_strength_plan_id_seq'::regclass) |  |
| `plan_date` | `date` | no |  |  |
| `status` | `text` | no | 'planned'::text | planned -> done (reconciled to an actual) \| skipped (passed, none) \| unplanned (did it w/o a plan; reconciler-created). Forward-only. Counts toward the target when status IN (done,unplanned). |
| `plan` | `jsonb` | yes |  | Day-of detail JSONB: {"exercises":[{name,watch_label,location,garmin,sets,reps:{low,high},reps_per_side,rest_s:{low,high},weight:{kg_low,kg_high,basis},note,fit:{reps,rest_s,weight_kg}}],"focus":...,"estimated_minutes":...,"rationale":...,"model":...}. NULL at scaffold; regenerated + pushed to Garmin day-of. |
| `garmin_workout_id` | `text` | yes |  | Garmin Connect id of the workout PUSHED to B's watch (set when pushed, before she trains). NULL if not pushed. Distinct from completion. |
| `completed_strength_session_id` | `integer` | yes |  | Loose FK to exercise.strength_sessions — the ACTUAL session, set by the reconciler after. NULL until done. |
| `meta` | `jsonb` | no | '{}'::jsonb | Provenance: {"model":...,"factors":{"sleep_h":..,"days_since_cardio":..}}. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation. |

### Table: `exercise.strength_sessions`
One row per strength training session. Source-agnostic: source_app + source_activity_id identify the origin platform. inbound_row_id is a loose FK into whichever raw inbound table applies (system.garmin_inbound today).

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `strength_session_id` | `integer` | no | nextval('exercise.strength_sessions_strength_session_id_seq'::regclass) |  |
| `strava_inbound_id` | `integer` | yes |  | Loose FK to system.strava_inbound. NULL if session did not come via a Strava webhook trigger (e.g. manual entry or future direct-source ingestion). |
| `strava_activity_id` | `bigint` | yes |  | Strava-side activity ID. NULL if not Strava-triggered. |
| `source_app` | `text` | no | 'garmin'::text | Platform that recorded the session. garmin = Garmin Connect. hevy = HEVY app. manual = logged directly. |
| `inbound_row_id` | `integer` | yes |  | Loose FK into the relevant raw inbound table — garmin_inbound_id when source_app=garmin. No PG foreign key because the target table varies by source. |
| `source_activity_id` | `text` | yes |  | Activity ID in the source platform. TEXT to accommodate non-integer IDs from future sources. |
| `activity_name` | `text` | yes |  | Session name as recorded in the source platform, e.g. Full Body (Set A). |
| `started_at` | `timestamp with time zone` | no |  | UTC timestamp when the session began. |
| `duration_seconds` | `integer` | yes |  | Total elapsed session duration in seconds, as reported by the source platform. |
| `avg_hr` | `numeric(5,1)` | yes |  | Average heart rate across the full session, from the source platform summary. |
| `max_hr` | `numeric(5,1)` | yes |  | Peak heart rate recorded during the session. |
| `calories_kcal` | `integer` | yes |  | Active calories burned as reported by the source platform. NULL when not available. |
| `total_active_sets` | `integer` | yes |  | Count of ACTIVE sets only. REST periods are not counted. |
| `total_exercises` | `integer` | yes |  | Count of distinct exercise names recorded in the session. |
| `perceived_exertion` | `integer` | yes |  | RPE 1–10, B-reported via Telegram. NULL until reported. Not device-recorded. |
| `device_name` | `text` | yes |  | Name of the recording device, e.g. Garmin Forerunner 265. NULL when not available from source. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source-specific fields not promoted to dedicated columns. Keeps schema stable as source platforms evolve. |
| `created_at` | `timestamp with time zone` | no | now() | Row creation timestamp (UTC). |
| `updated_at` | `timestamp with time zone` | yes |  | Last update timestamp (UTC). Set on any correction or re-sync. |

### Table: `exercise.strength_sets`
One row per active set in a strength session. REST periods from Garmin are folded into rest_seconds_after on the preceding active set — not stored as separate rows. Source-agnostic: source_app on the parent session identifies whether data came from Garmin, HEVY, etc.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `strength_set_id` | `integer` | no | nextval('exercise.strength_sets_strength_set_id_seq'::regclass) |  |
| `strength_session_id` | `integer` | no |  | FK → exercise.strength_sessions. Cascades on delete. |
| `set_index` | `integer` | no |  | 1-based position of this active set within the session, in chronological order. First set = 1. REST rows from Garmin are skipped when numbering. |
| `exercise_name` | `text` | yes |  | Exercise label as reported by the source app. For Garmin: highest-probability candidate from the on-device ML classifier (e.g. GOBLET_SQUAT). For HEVY: user-defined exercise name. |
| `exercise_category` | `text` | yes |  | Broad movement category as reported by the source app. For Garmin: the category field returned by get_activity_exercise_sets (e.g. STRENGTH_TRAINING). May differ across sources — do not assume a controlled vocabulary until a catalog table is introduced. |
| `reps_recorded` | `integer` | yes |  | Rep count recorded by the device. NULL for timed sets (planks etc.) or if device did not record reps. |
| `weight_recorded` | `numeric(10,3)` | yes |  | Weight as recorded by the device, in the unit given by weight_recorded_unit. Garmin stores grams internally; we convert to kg on write so weight_recorded_unit is always kg for Garmin rows. Derive kg and lb values via CASE when needed. |
| `weight_recorded_unit` | `text` | yes |  | Unit of weight_recorded. 'kg' or 'lb'. Garmin rows are always 'kg' (converted from grams at write time). HEVY rows may be 'lb' if the user logs in imperial. |
| `duration_seconds` | `numeric(7,2)` | yes |  | Active set duration in seconds. Populated for timed sets (e.g. planks, wall sits). NULL for rep-based sets where duration was not tracked. |
| `rest_seconds_after` | `numeric(7,2)` | yes |  | Rest time in seconds between this set and the next active set. Derived from Garmin REST-type rows that immediately follow this set. NULL if no rest was recorded or this is the last set. |
| `started_at` | `timestamp with time zone` | yes |  | Wall-clock start time of this set. From Garmin startTime field. NULL if source did not provide per-set timestamps. |
| `avg_hr_during_set` | `numeric(5,1)` | yes |  | Average heart rate in bpm during the active set, as reported by the device. NULL if HR was not recorded or device lacked a heart rate sensor at set time. |
| `max_hr_during_set` | `numeric(5,1)` | yes |  | Peak heart rate in bpm during the active set, as reported by the device. NULL if HR was not recorded or device lacked a heart rate sensor at set time. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source-app-specific fields that do not fit the normalised columns. For Garmin: full exercise candidate list with probability scores (e.g. [{'exercise': 'GOBLET_SQUAT', 'probability': 0.996}, ...]). For HEVY: superset_id, notes, etc. Schema varies by source_app. |
| `reps_reported` | `integer` | yes |  | Self-reported rep count from B via Telegram quote-reply correction. Overrides reps_recorded for display; device value is preserved. |
| `weight_reported` | `numeric(10,3)` | yes |  | Self-reported weight from B via Telegram, in the unit given by weight_reported_unit. NULL until a correction arrives. |
| `weight_reported_unit` | `text` | yes |  | Unit of weight_reported. 'kg' or 'lb'. Preserved exactly as B stated (e.g. '25 lb each hand' → weight_reported=25, weight_reported_unit='lb'). |
| `reported_at` | `timestamp with time zone` | yes |  | Timestamp when B submitted the Telegram correction. NULL until a correction arrives. |
| `reported_meta` | `jsonb` | no | '{}'::jsonb | Raw extracted correction data from B's Telegram message. Stores the original text and any structured fields the LLM extracted (e.g. '25kg each hand', notes). Useful for debugging extraction quality. |
| `created_at` | `timestamp with time zone` | no | now() | Row creation timestamp. Set once on insert. |
| `updated_at` | `timestamp with time zone` | yes |  | Last update timestamp. Set on every correction write. |

## Schema: `external_data`

### View: `external_data.menu_current`
Most recent successful menu batch per restaurant. Use this for agent meal-planning queries. Partial-failure tolerant: if a shop fails this run, its last-good batch is still returned. item_name_en holds the best available name — English from source when available, Thai script otherwise.

**View definition:**
```sql
SELECT restaurant_name,
    item_name_en,
    category,
    price_sgd,
    price_thb,
    kcal,
    protein_g,
    carbs_g,
    fat_g
   FROM external_data.menu_items m
  WHERE scraped_at = (( SELECT max(menu_items.scraped_at) AS max
           FROM external_data.menu_items
          WHERE menu_items.restaurant_name = m.restaurant_name));
```

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `restaurant_name` | `text` | yes |  |  |
| `item_name_en` | `text` | yes |  |  |
| `category` | `text` | yes |  |  |
| `price_sgd` | `numeric(8,4)` | yes |  |  |
| `price_thb` | `numeric(8,2)` | yes |  |  |
| `kcal` | `numeric(7,2)` | yes |  |  |
| `protein_g` | `numeric(6,2)` | yes |  |  |
| `carbs_g` | `numeric(6,2)` | yes |  |  |
| `fat_g` | `numeric(6,2)` | yes |  |  |

### Table: `external_data.menu_items`
Append-only restaurant menu snapshots. Every scrape run appends a new batch; rows never change after insert. To read the latest menu per restaurant, filter by max(scraped_at) per restaurant_name.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `menu_item_id` | `bigint` | no | nextval('external_data.menu_items_menu_item_id_seq'::regclass) | Surrogate primary key. |
| `source` | `text` | no |  | Scraper that produced this row. Values: fitfuel, jones, wongnai. |
| `restaurant_name` | `text` | no |  | Canonical English restaurant name — hardcoded in scraper config, never taken from the page. Consistent across every scrape. Grouping key for latest-menu queries. |
| `item_name_en` | `text` | yes |  | Best available item name — English when the source provides it (FitFuel, Jones smoothies, some WongNai items), Thai script otherwise (most WongNai shops, Jones Salad food rows). Translation is deferred to agent time, not performed at scrape time. |
| `category` | `text` | yes |  | Menu section as labelled by the source. Not normalised across sources. |
| `price_thb` | `numeric(8,2)` | yes |  | Price in Thai Baht. Set for WongNai and FitFuel. NULL for Jones Salad (no prices published) and future Singapore menus. |
| `price_sgd` | `numeric(8,4)` | yes |  | Price in Singapore Dollars. For Thailand menus: price_thb converted at the THB/SGD spot rate fetched once per scrape run (rate stored in meta). For future Singapore menus: the native price. NULL if FX fetch failed. |
| `kcal` | `numeric(7,2)` | yes |  | Energy in kilocalories. |
| `protein_g` | `numeric(6,2)` | yes |  | Protein in grams. |
| `carbs_g` | `numeric(6,2)` | yes |  | Carbohydrates in grams. |
| `fat_g` | `numeric(6,2)` | yes |  | Fat in grams. |
| `fibre_g` | `numeric(6,2)` | yes |  | Dietary fibre in grams. |
| `sugar_g` | `numeric(6,2)` | yes |  | Sugar in grams. |
| `sodium_mg` | `numeric(7,2)` | yes |  | Sodium in milligrams. |
| `meta` | `jsonb` | no | '{}'::jsonb | Source-specific extras. For Thailand menus includes fx_rate_thb_sgd, fx_source, fx_fetched_at. For FitFuel includes dish_id, dietary_flags, allergens. For WongNai includes wongnai_shop_id and Leanlicious LINE enrichment fields. |
| `scraped_at` | `timestamp with time zone` | no |  | Timestamp shared by all rows in one scrape run. Acts as the batch identifier — use max(scraped_at) per restaurant_name to get the current menu. |
| `created_at` | `timestamp with time zone` | no | now() | Row insert timestamp. |

## Schema: `finances`

### Table: `finances.fx_lot_allocations`
Links cash/TrueMoney foreign-currency spends to the fx_lots consumed for SGD cost basis. One spend may consume multiple lots (multi-row set when one lot has insufficient remaining balance). Used only for payment_method in (cash, truemoney). YouTrip and other wallet/card paths use market-rate estimate on the spend row, with breakdown (when blended) in spend_entries.source_meta.fx_rate_breakdown instead of allocation rows. Corrections delete-and-recreate all rows for a spend in a single transaction — do not patch individual allocation rows.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `fx_lot_allocation_id` | `integer` | no | nextval('finances.fx_lot_allocations_fx_lot_allocation_id_seq'::regclass) |  |
| `spend_entry_id` | `integer` | no |  | FK to finances.spend_entries. ON DELETE CASCADE so spend deletion cleans allocations. |
| `fx_lot_id` | `integer` | no |  | FK to finances.fx_lots — the lot consumed. |
| `allocated_amount` | `numeric(12,2)` | no |  | Foreign-currency amount consumed from the lot for this spend. Currency is derivable via JOIN to fx_lots.target_currency_code. |
| `allocated_sgd_amount` | `numeric(6,2)` | no |  | SGD cost basis consumed from the lot for this spend = allocated_amount * (fx_lots.sgd_cost_amount / fx_lots.target_amount). Sum across rows for a spend equals spend_entries.sgd_amount. |
| `created_at` | `timestamp with time zone` | no | now() |  |

### Table: `finances.fx_lots`
Foreign-currency acquisition pool. One row per batch B obtained (e.g. exchanging SGD for THB at SuperRich). Records acquisition only — not spending. FIFO consumption order is (target_currency_code, acquired_at, fx_lot_id). Cash and TrueMoney share the same pool in v1 (no separate TrueMoney wallet model). Supports any target currency. Remaining balance = target_amount - SUM(fx_lot_allocations.allocated_amount); computed at read time, not stored.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `fx_lot_id` | `integer` | no | nextval('finances.fx_lots_fx_lot_id_seq'::regclass) |  |
| `acquired_at` | `timestamp with time zone` | no |  | When the foreign currency was acquired. Required for FIFO ordering. |
| `provider_name` | `text` | yes |  | Loose human label for the money changer. Examples: "SuperRich", "Arcade money changer", "Little India money changer", "Chinatown money changer". Free text; not normalised. |
| `source_currency_code` | `text` | no |  | ISO 4217 of the currency B paid in. Usually SGD. |
| `source_amount` | `numeric(12,2)` | no |  | Amount paid in source_currency_code. NUMERIC(12,2) — same precision as transaction_amount on spend_entries for symmetry. |
| `target_currency_code` | `text` | no |  | ISO 4217 of the currency acquired. Examples: THB, JPY, EUR. |
| `target_amount` | `numeric(12,2)` | no |  | Amount of target currency acquired (the FIFO pool fills this). |
| `sgd_cost_amount` | `numeric(6,2)` | no |  | SGD cost basis for the lot. Equals source_amount when source_currency_code=SGD. NUMERIC(6,2) caps at S$9,999.99 — same as spend cap. Larger exchanges entered via SQL directly. Effective rate = sgd_cost_amount / target_amount, derived in views. |
| `notes` | `text` | yes |  | Freeform B-facing notes: caveats, location, photo description, etc. For Telegram-sourced rows, app code defaults this to the original message text. |
| `source_meta` | `jsonb` | yes |  | Provenance and parser metadata blob. Conventional keys: source_type (text/voice/photo/superrich_receipt/manual), source_reference (receipt number, if any), channel (telegram/manual), telegram_update_id, telegram_file_id, model, vision_parser_version. No secrets. |
| `created_at` | `timestamp with time zone` | no | now() |  |
| `updated_at` | `timestamp with time zone` | yes |  |  |

### Table: `finances.spend_entries`
One row per finance event B is tracking — real spend, pending candidate, or recognised non-spend (topup, bill payment, transfer). Grain: one transaction. SGD is the home currency, capped at NUMERIC(6,2) = max S$9,999.99 per row. Larger spends are out of scope for bot logging — insert via SQL directly. status and missing_fields are not stored; derived in domains/expense/types.py for follow-up decisions and in marts views for reporting. All inbound provenance lives in source_meta — no FK columns.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `spend_entry_id` | `integer` | no | nextval('finances.spend_entries_spend_entry_id_seq'::regclass) |  |
| `spent_at` | `timestamp with time zone` | no |  | When the transaction occurred. Source timestamp when known (Grab email, OCBC posting, receipt). Telegram text without a stated date: time of the Telegram message in B local time. Relative ("2 days ago"): LLM-resolved local timestamp. Date-only sources: noon local. Bot reply shows resolved value for correction. |
| `ignored_reason` | `text` | yes |  | Non-NULL marks a recognised non-spend. Known values: youtrip_topup, credit_card_bill_payment, transfer, duplicate, not_spend, fx_acquisition (money-changer slip / FX acquisition). Free text validated in app code (IGNORED_REASONS), no CHECK. |
| `merchant_name_raw` | `text` | yes |  | Shop or recipient the money went to. Stored as observed — not normalised. Examples: "McDonald's" (GrabFood order), "Cold Storage", "YOU TECHNOLOGIES GROUP" (PayNow narrative for YouTrip topup). For GrabFood / Line Man / Bolt orders, this is the underlying merchant, not the platform. |
| `platform` | `text` | yes |  | Delivery / marketplace layer between B and merchant. Examples: Grab, Line Man, Bolt, Foodpanda, Klook. NULL when B transacted directly. Never a payment processor (those go in payment_method). |
| `category` | `text` | yes |  | Expense category, free text. Vocabulary in domains/expense/types.py, evolves over time. Initial set: food, transport, groceries, healthcare, personal_care, utilities, shopping, travel, fitness, supplements, beauty, entertainment, gifts, education, home, subscriptions, ignored, unknown. Quoted-reply correction can change this on any row. |
| `notes` | `text` | yes |  | Freeform prose: what was bought, why, any B caveat. For Telegram-sourced rows, app code defaults this to the original Telegram message text (or caption for photos) so the raw input is always preserved on the row. Pairs with items_json when a receipt is itemised. |
| `items_json` | `jsonb` | yes |  | Structured bill breakdown (JSONB), null when not itemised. v2: {currency, lines:[{name (English), name_local (as printed / null), qty, unit, modifiers[], unit_price, amount}], adjustments:[{kind in (fee,discount,tax,service_charge,tip,deposit,rounding,other),label,amount signed}], subtotal, total}. total = subtotal + sum(adjustments) = transaction_amount. Names are English; name_local keeps the original printed name. Legacy rows may hold {line_items,fees,discounts} or a flat array. |
| `transaction_currency_code` | `text` | yes |  | ISO 4217 of the original transaction. Examples: SGD, THB, USD, JPY. May differ from payment_method home currency (e.g. HSBC SGD card charged in USD). |
| `transaction_amount` | `numeric(12,2)` | yes |  | Original amount in transaction_currency_code. NUMERIC(12,2) for room (foreign currencies — THB tens of thousands for travel spends). |
| `sgd_amount` | `numeric(6,2)` | yes |  | Home-currency amount in SGD. The single number every report sums. NUMERIC(6,2) caps at S$9,999.99 per row — larger spends are deliberate enough to enter via SQL directly. Effective FX rate is derived in views as sgd_amount / transaction_amount. |
| `fx_rate_source` | `text` | yes |  | How sgd_amount was determined. Values: not_applicable_sgd (transaction in SGD), actual_ocbc, actual_youtrip, actual_superrich_fifo (FIFO from fx_lots — breakdown in fx_lot_allocations rows), frankfurter_estimate (daily ECB reference), manual (B stated SGD amount or rate), mixed (blended non-lot sources — breakdown in source_meta.fx_rate_breakdown), unknown. Never hallucinated. |
| `fx_rate_observed_at` | `timestamp with time zone` | yes |  | When the FX rate snapshot was taken. For actual_* sources: receipt/posting timestamp (typically equals spent_at). For frankfurter_estimate: ECB publication date used (will be earlier than spent_at). Stored even when equal to spent_at for explicit auditability. NULL when unknown. |
| `payment_method` | `text` | yes |  | How B paid. CHECK-constrained vocabulary. Adding a new method requires ALTER ... ADD CHECK. |
| `source_meta` | `jsonb` | yes |  | Provenance and parser metadata blob. Conventional keys: source_type (text/voice/photo/correction/manual/grab/bolt/line_man/foodpanda/klook/ocbc_promptpay/paynow_email/paylah_email/hsbc_statement/youtrip_screenshot/youtrip_email/superrich_receipt/generic_receipt — semantic source, channel-independent), source_reference (external transaction ID for dedup: Grab booking ID, OCBC ref, PayNow ref, SuperRich receipt number, YouTrip tx ID), channel (telegram/gmail/manual), telegram_update_id, telegram_file_id, gmail_message_id, gmail_inbound_id, model, model_confidence, vision_parser_version, language_detected, transcription_used, fx_rate_breakdown (when fx_rate_source=mixed; see comment on fx_rate_source). No secrets, no raw email bodies, no full card numbers. |
| `created_at` | `timestamp with time zone` | no | now() |  |
| `updated_at` | `timestamp with time zone` | yes |  |  |

## Schema: `health_agent`

### Table: `health_agent.daily_plan`
The 7-day spine: one row per planned day. Holds the day's activity kinds, the meal shop + the vegetarian flag, the cached macro target, the meal-spend link, and B's free-text notes. Exercise detail lives in exercise.strength_plan / cardio_plan; meals in nutrition.meal_plan.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `daily_plan_id` | `integer` | no | nextval('health_agent.daily_plan_daily_plan_id_seq'::regclass) |  |
| `plan_date` | `date` | no |  | Calendar day. UNIQUE; satellites FK to this. |
| `activity_type` | `text[]` | no | ARRAY['rest'::text] | The day's activities (array; supports a 2-a-day e.g. {strength,cardio}). Vocab: rest \| cardio (run/hash/hike/cycle/swim — all count toward the cardio target) \| strength \| other (yoga/pilates/climbing — no satellite). A strength_plan/cardio_plan row may only exist when the matching value is present (enforced by trigger). |
| `is_vegetarian_day` | `boolean` | yes |  | TRUE on the single vegetarian day the Sun scaffold picks each Mon-Fri week (B can move it). Set week-ahead so /week shows it BEFORE meals are planned day-of; the 11am planner honours it. |
| `macro_target` | `jsonb` | yes |  | Derived cache of the day's macro budget: {"kcal":{low,target,high},"protein_g":{low,high},"fat_g":{min},"carbs_g":{target},"fibre_g":{target,stretch},"day_type"}. kcal low/high = target ± kcal_band_kcal (125); carbs = remainder (no floor). Recomputed when activity changes + refreshed day-of. NULL until computed. |
| `meal_plan_provider` | `text` | yes |  | The day's ONE shop (same_shop). Canonical external_data menu restaurant_name. NULL on own-food/weekend. |
| `meal_spend_id` | `integer` | yes |  | Loose link to finances.spend_entries — the day's meal order. Set by the expense reconciler when a meal-category spend's merchant matches meal_plan_provider. ON DELETE SET NULL. |
| `notes` | `jsonb` | no | '[]'::jsonb | B's free-text edits as a timestamped array: [{"at":ts,"text":str,"active":bool,"kind":"pin\|context"}]. The edit handler (Flash) auto-classifies: a PIN changes the day's activity/shop and deterministically LOCKS that day (a re-plan/scaffold must NOT move a day with an active pin — the pin IS the lock signal, no separate column); a CONTEXT note only informs the LLM. Notes are ONE-OFF (this week; treated as expired once the horizon passes — no standing rules, those get coded directly). An active pin outranks every other constraint. Editable/removable via Telegram; shown as a note line in /week + /plan week. |
| `meta` | `jsonb` | no | '{}'::jsonb | Provenance/debug, e.g. {"model":"...","scaffold_run_id":"..."}. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation; set ONLY on a real change (never a blanket update). |

### Table: `health_agent.weekly_reflections`
One row per ISO week. The Sun cron computes maintenance (mean intake over flat-weight weeks) + the 3-4wk weight trend, sets next week's target (= maintenance − a band-aware deficit), and writes the narrative + carry-forward directives. No 7700, no Garmin, no kcal/kg, no regression — maintenance is the flat-week intake MEAN (the regression intercept; PERIOD weeks excluded); the band controller + 3-4wk trend pick cut/hold/gain. Frozen during cuts (carries last value), re-measures when stable.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `weekly_reflection_id` | `integer` | no | nextval('health_agent.weekly_reflections_weekly_reflection_id_seq'::regclass) |  |
| `iso_week` | `text` | no |  | ISO year-week, format IYYY-"W"IW (e.g. 2026-W26, 2026-W52, 2027-W01). UNIQUE. MUST use IYYY (ISO-year) not YYYY (calendar-year): they diverge in late-Dec/early-Jan, so YYYY would mislabel the turn-of-year week. Rolls into next year with no collisions since the ISO year is in the key. |
| `maintenance_kcal` | `integer` | yes |  | Mean daily intake over trailing flat-weight weeks (\|Δ7d-avg\| < 0.2 kg). |
| `target_kcal` | `integer` | yes |  | Daily weekly-AVERAGE target set for next week (redistributed across cardio/strength/rest days). |
| `weight_trend_kg` | `numeric(4,2)` | yes |  | 3-4wk slope of the 7d-avg weight (kg/week) — the calibration anchor. |
| `narrative` | `text` | yes |  | Prose B reads. |
| `directives` | `jsonb` | no | '{}'::jsonb | Machine carry-forward for the daily planners (cautions, focus). |
| `meta` | `jsonb` | no | '{}'::jsonb | Audit of the inputs. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation. |

## Schema: `nutrition`

### Table: `nutrition.food_log`
One row per distinct food item. A single message from B may produce one or multiple rows — the system parses the input and inserts one row per identifiable item. A described combo ("2 eggs, yoghurt, blueberries") becomes 3 rows; a single named dish ("Viking chicken wrap") becomes 1.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `food_log_id` | `integer` | no | nextval('nutrition.food_log_food_log_id_seq'::regclass) |  |
| `meal_type` | `text` | no |  | Meal slot. Values: breakfast, brunch, lunch, snack, dinner, supper, pre_workout, post_workout. |
| `telegram_update_id` | `bigint` | yes |  | Telegram update_id of the inbound message that triggered this row. Joins to system.telegram_inbound.update_id. NULL for system-inserted rows. |
| `food_item` | `text` | no |  | Free-text description of the food item as logged (e.g. "2 boiled eggs", "Greek yoghurt 150g"). |
| `food_meta` | `jsonb` | yes |  | Optional structured metadata. Shape: {"qty": {"amount": 150, "unit": "g"}, "prep": "grilled", "brand": "Chobani", "notes": "free text"}. All keys optional. qty.amount is numeric; qty.unit is a string (g, ml, pieces, cups, etc.). |
| `kcal` | `numeric(7,2)` | yes |  | Kilocalories. NULL if unknown. |
| `protein_g` | `numeric(6,2)` | yes |  | Protein in grams. NULL if unknown. |
| `carbs_g` | `numeric(6,2)` | yes |  | Total carbohydrates in grams. NULL if unknown. |
| `fat_g` | `numeric(6,2)` | yes |  | Total fat in grams. NULL if unknown. |
| `fibre_g` | `numeric(6,2)` | yes |  | Dietary fibre in grams. NULL if unknown. |
| `sugar_g` | `numeric(6,2)` | yes |  | Total sugar in grams. NULL if unknown. |
| `sodium_mg` | `numeric(7,2)` | yes |  | Sodium in milligrams. NULL if unknown. |
| `source` | `text` | no | 'telegram'::text | How the row was created. Values: telegram, system. |
| `macro_input` | `text` | no |  | Nature of the input used to derive macros. Values: nutrition_label (packaged food label panel with full government-mandated rows incl. sodium, may need pro-rating), macro_screenshot (printed macro display without full nutrition panel — meal service card, restaurant menu, app screenshot), restaurant_reported (restaurant or meal plan published numbers), description (B described food via text or voice), image (food photo sent for visual estimation), manual (B provided numbers directly). |
| `macro_method` | `text` | no |  | Tool or source used to derive the macro values. Values: nutrition_label (read directly from packaged food label panel), restaurant_reported (numbers published or printed by a brand or meal service — includes macro_screenshot photos of meal cards, restaurant menus, and app screenshots), usda (USDA FoodData Central), open_foods (Open Food Facts), edamam (Edamam API), llm (model estimated), manual (B provided numbers directly via text). |
| `macro_meta` | `jsonb` | yes |  | Method-specific provenance detail. Shape varies by macro_method — llm: {"model": "gemini-2.5-flash"}; nutrition_label: {"model": "...", "file_id": "<telegram file_id>", "field_sources": {"fibre_g": {"source": "nutrition_label", "status": "zero_from_label"}}}; restaurant_reported (macro_screenshot): {"model": "...", "file_id": "...", "field_sources": {"kcal": {"status": "from_source", "source": "macro_screenshot"}, "fibre_g": {"status": "gap_filled", "model": "gemini-2.5-flash"}}}; usda: {"fdc_id": "...", "description": "..."}; open_foods: {"barcode": "...", "product_name": "..."}; edamam: {"food_id": "...", "label": "..."}; manual: null or {"user_stated_fields": [...], "correction_update_id": N}. |
| `created_at` | `timestamp with time zone` | no | now() | Row insertion timestamp. Set automatically; not edited after insert. |
| `protein_source` | `text[]` | yes |  | Main protein(s) actually eaten in this entry — only a MAIN protein (>=~15 g, or the dish centrepiece; garnish like bacon bits is ignored), including B's own outside meals. Vocab: beef, pork, chicken, duck, lamb, mutton, fish, other_seafood, egg, cheese, plant_other. The PRESENCE-based protein rotation (beef>=1, pork>=1, fish 1-2, duck>=1/2wk, 1 veg day) tallies lunch/dinner mains from HERE — only fish counts toward the fish slot; other_seafood (shellfish/prawns/squid) is logged but does NOT count. NOTE: the >=10 eggs/wk rule counts egg QUANTITY from food_log items, NOT from this array — text[] is presence-only and holds no count. |

### Table: `nutrition.meal_plan`
A planned lunch/dinner (one row per (plan_date, meal_type)). Born 'planned' at 11am; flips 'bought' when a matching meal spend logs; 'ate' on confirm (posts items to food_log); 'skipped' by the next-day 11am sweep. Status is FORWARD-ONLY (eat before logging spend -> stays ate). Shop is day-level on daily_plan.meal_plan_provider; protein lives per-item in items.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `meal_plan_id` | `integer` | no | nextval('nutrition.meal_plan_meal_plan_id_seq'::regclass) |  |
| `plan_date` | `date` | no |  |  |
| `meal_type` | `text` | no |  | lunch \| dinner. UNIQUE per (plan_date, meal_type). |
| `status` | `text` | no | 'planned'::text | planned -> bought (spend matches) -> ate (confirmed; posts food_log) \| skipped (sweep). bought ⇒ auto-ate at the sweep (paid ≈ eaten). Never regresses. |
| `items` | `jsonb` | no | '[]'::jsonb | Chosen items + home staples, macros SNAPSHOTTED at plan time (menu is a re-scraped view, no stable id): [{"item_name","restaurant","kcal","protein_g","carbs_g","fat_g","price_thb","protein_source","role":"main\|staple\|side"}]. role=staple = an always-stocked fridge item (egg/yoghurt/milk/edamame/natto), composed co-equally with shop dishes but shop-independent (restaurant NULL). A staple is STORED in the lunch or dinner items[] it accompanies, but logs INDEPENDENTLY: each staple gets its OWN ✓ Ate button (separate from the meal's), posting only that staple to food_log; per-staple posted state is tracked in meta (idempotent). The protein-rotation tally reads food_log.protein_source (actual), not here. |
| `posted_food_log_ids` | `integer[]` | yes |  | All food_log rows posted for this slot — the main meal plus any tapped staples (idempotent undo). Which individual items have posted is tracked in meta (per-staple ✓ Ate buttons log independently). |
| `meta` | `jsonb` | no | '{}'::jsonb | Provenance: {"model":...}. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |
| `updated_at` | `timestamp with time zone` | yes |  | Last mutation. |

## Schema: `system`

### Table: `system.conversation_state`
One row per bot reply that may participate in a quoted correction chain. Root rows are written for every bot reply to a loggable action (food, weight, expense, attention, aligner). Follow-up rows are written when B quotes a bot reply and a correction is applied. Full thread is rebuilt via recursive CTE joining telegram_outbound and telegram_inbound. context holds only the minimal structured state needed for the next correction turn.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_reply_message_id` | `integer` | no |  | Telegram message_id of this bot reply. References system.telegram_outbound(message_id). |
| `parent_telegram_reply_message_id` | `integer` | yes |  | Bot reply B quoted when triggering this correction round. NULL for root rows (initial log reply). |
| `triggering_telegram_update_id` | `bigint` | no |  | Inbound update_id that caused this bot reply. References system.telegram_inbound(update_id). Used to reconstruct user text from telegram_inbound.payload. |
| `domain` | `text` | no |  | Domain that owns the quoted-reply correction chain. CHECK-constrained vocabulary: food, attention, aligner, weight, sleep_wake, expense, query, plan. (plan = 7-day planner edits: context = {"plan_date_range":[...],"plan_day_ids":[...],"planned_run_ids":[...],"meal_plan_ids":[...]}.) Add a value here and in this comment when a new domain saves correction state. |
| `context` | `jsonb` | no |  | Domain-specific structured data for the correction chain. food: {"food_log_ids":[int],"meal_type":str}. attention: {"attention_session_ids":[int]}. aligner (wear-event reply): {"aligner_wear_event_ids":[int],"kind":"out"\|"in"\|"out_guard"\|"updated"}. aligner (tray reply): {"aligner_tray_change_ids":[int],"arch":"upper"\|"lower","kind":"tray"}. weight: {"weight_measurement_ids":[int]}. sleep_wake: {"sleep_wake_event_ids":[int],"event_type":"sleep"\|"wake","auto_inferred":bool}. expense: {"spend_entry_id":int}. plan: {"kind":"meal"\|"run"\|"week","plan_date":date,...} — the agentic planner correctors (meal swap / run+strength re-gen / week edit), routed by kind. |
| `created_at` | `timestamp with time zone` | no | now() | Insertion time. |

### Table: `system.garmin_inbound`
Raw payloads fetched from Garmin Connect. One row per successful fetch (no upsert) so we have full history of what Garmin returned, including any schema changes over time.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `garmin_inbound_id` | `integer` | no | nextval('system.garmin_inbound_garmin_inbound_id_seq'::regclass) |  |
| `object_id` | `bigint` | no |  | Garmin activityId. Not unique — a single activity may be fetched multiple times (e.g. backfill + live capture). |
| `payload` | `jsonb` | no |  | JSON shape: {"summary": <activity_detail>, "exercise_sets": <exerciseSets response>, "hr_samples": [[timestamp_ms, hr_bpm], ...]}. hr_samples is the second-by-second HR stream from the details endpoint; may be absent for rows captured before that field was added. |
| `strava_inbound_id` | `integer` | yes |  | FK to the Strava webhook event that triggered this fetch. NULL for backfill or manual CLI runs. |
| `received_at` | `timestamp with time zone` | no | now() |  |
| `source` | `text` | no |  | Why this fetch happened. strava_trigger = Strava webhook fired and we looked up the matching Garmin activity. backfill = one-off historical sync. manual = ad-hoc CLI run. |

### Table: `system.garmin_tokens`
Single-row cache of the Garmin Connect DI OAuth2 token blob. Written by the garmin-health-data CLI bootstrap (garmin auth). di_token (~18h) is refreshed automatically; di_refresh_token rotates on each refresh (~30d). Re-bootstrap needed only if refresh_token expires.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `garmin_token_id` | `integer` | no | 1 |  |
| `token_blob` | `jsonb` | no |  | JSON: {"auth_mode": "garmin_health_data", "di_token": "<jwt>", "di_refresh_token": "<base64>", "di_client_id": "GARMIN_CONNECT_MOBILE_ANDROID_DI_2025Q2"}. Upserted on every token refresh. |
| `updated_at` | `timestamp with time zone` | no | now() |  |

### Table: `system.pinned_messages`
One row per pin "kind" we keep alive in the Telegram chat. Lets the 11am meal pin and the 1pm exercise pin coexist (Telegram supports multiple pins) while each self-replaces within its kind. pin_kept(kind,new_id) unpins the stored message_id for that kind, pins new_id, upserts the row. Keyed on kind (not date) so today's meal pin replaces yesterday's. Replaces the getChat-based single-pin path for these two flows only.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `kind` | `text` | no |  |  |
| `chat_id` | `bigint` | no |  |  |
| `message_id` | `bigint` | no |  |  |
| `updated_at` | `timestamp with time zone` | no | now() |  |

### Table: `system.strava_inbound`
Every inbound Strava webhook event received by the app, stored as raw JSON. One row per event delivery. Written before any processing so nothing is lost even if processing fails. Mirrors the pattern of system.telegram_inbound. aspect_type in payload distinguishes create, update, and delete events for the same object_id.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `strava_inbound_id` | `integer` | no | nextval('system.strava_inbound_strava_inbound_id_seq'::regclass) | Surrogate primary key. |
| `object_id` | `bigint` | no |  | Strava activity or object ID from the webhook payload. Multiple rows may share the same object_id when Strava sends create, update, and delete events for the same activity. Joins to exercise tables via strava_activity_id when those tables exist. |
| `payload` | `jsonb` | no |  | Full raw Strava webhook event JSON as received. Contains object_type (e.g. activity), object_id, aspect_type (create, update, delete), owner_id, subscription_id, and event_time. |
| `received_at` | `timestamp with time zone` | no | now() | Time the event was received by the webhook. |

### Table: `system.telegram_inbound`
Every inbound Telegram update received by the webhook, stored as raw JSON. One row per update_id. Written before any processing so nothing is lost even if routing fails. Counterpart to system.telegram_outbound.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_inbound_id` | `integer` | no | nextval('system.telegram_inbound_id_seq'::regclass) | Surrogate primary key. |
| `update_id` | `bigint` | no |  | Telegram-assigned ID for this webhook delivery event. Unique per bot globally. Joins to domain tables e.g. nutrition.food_log.telegram_update_id and to system.telegram_outbound.telegram_update_id. |
| `payload` | `jsonb` | no |  | Full raw Telegram Update JSON as received. |
| `received_at` | `timestamp with time zone` | no | now() | Time the update was received by the webhook. |

### Table: `system.telegram_outbound`
Every message the bot sends to Telegram, logged immediately after the API call returns. payload stores the full JSON body sent to the Telegram API — covers text, inline keyboards, photos, and any future message type without schema changes. Counterpart to system.telegram_inbound.

| Column | Type | Nullable | Default | Notes |
|--------|------|----------|---------|-------|
| `telegram_outbound_id` | `integer` | no | nextval('system.telegram_outbound_telegram_outbound_id_seq'::regclass) | Surrogate primary key. |
| `message_id` | `integer` | no |  | Telegram-assigned message_id for this sent message, from the API response result.message_id. Used to look up this row when B quotes the bot reply. |
| `telegram_update_id` | `bigint` | yes |  | update_id of the inbound update that triggered this reply. Joins to system.telegram_inbound.update_id. Null for proactive bot messages. |
| `payload` | `jsonb` | no |  | Full JSON body sent to the Telegram API. Contains text, reply_parameters, inline_keyboard, parse_mode, etc. |
| `created_at` | `timestamp with time zone` | no | now() | Time the API call returned successfully. |
