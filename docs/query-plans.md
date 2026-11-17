# Query plans

Table: 10,500 rows in `quakes_earthquake`.

## Default list, first page

What every client hits first.

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE NOT "quakes_earthquake"."is_deleted" ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51
```

```
Limit  (cost=0.29..19.66 rows=51 width=140) (actual time=0.014..0.041 rows=51 loops=1)
  Buffers: shared hit=11
  ->  Index Scan using quake_visible_time_id_idx on quakes_earthquake  (cost=0.29..4061.07 rows=10687 width=140) (actual time=0.013..0.036 rows=51 loops=1)
        Buffers: shared hit=11
Planning:
  Buffers: shared hit=108
Planning Time: 0.659 ms
Execution Time: 0.083 ms
```

## Deep page via cursor (~10,496 rows in)

Cursor pagination: WHERE time < <cursor position>.

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE (NOT "quakes_earthquake"."is_deleted" AND "quakes_earthquake"."time" < '2018-07-25 14:44:42.980000+00:00'::timestamptz) ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51
```

```
Limit  (cost=0.29..8.30 rows=1 width=140) (actual time=0.012..0.013 rows=0 loops=1)
  Buffers: shared hit=2
  ->  Index Scan using quake_visible_time_id_idx on quakes_earthquake  (cost=0.29..8.30 rows=1 width=140) (actual time=0.011..0.011 rows=0 loops=1)
        Index Cond: ("time" < '2018-07-25 14:44:42.98+00'::timestamp with time zone)
        Buffers: shared hit=2
Planning:
  Buffers: shared hit=3
Planning Time: 0.114 ms
Execution Time: 0.036 ms
```

## Deep page via OFFSET (10,496)

What offset pagination would run for the same page. For contrast only: the API never does this.

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE NOT "quakes_earthquake"."is_deleted" ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51 OFFSET 10496
```

```
Limit  (cost=2376.27..2376.40 rows=51 width=140) (actual time=12.506..12.507 rows=1 loops=1)
  Buffers: shared hit=1534
  ->  Sort  (cost=2350.03..2376.75 rows=10687 width=140) (actual time=11.453..12.110 rows=10497 loops=1)
        Sort Key: "time" DESC, id DESC
        Sort Method: quicksort  Memory: 3048kB
        Buffers: shared hit=1534
        ->  Seq Scan on quakes_earthquake  (cost=0.00..1634.88 rows=10687 width=140) (actual time=0.018..7.159 rows=10497 loops=1)
              Filter: (NOT is_deleted)
              Rows Removed by Filter: 3
              Buffers: shared hit=1528
Planning Time: 0.051 ms
Execution Time: 12.780 ms
```

## Rare filter: min_mag=6

Highly selective: very few rows match.

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE (NOT "quakes_earthquake"."is_deleted" AND "quakes_earthquake"."mag" >= 6.0) ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51
```

```
Limit  (cost=140.07..140.17 rows=38 width=140) (actual time=0.038..0.040 rows=21 loops=1)
  Buffers: shared hit=23
  ->  Sort  (cost=140.07..140.17 rows=38 width=140) (actual time=0.038..0.038 rows=21 loops=1)
        Sort Key: "time" DESC, id DESC
        Sort Method: quicksort  Memory: 30kB
        Buffers: shared hit=23
        ->  Bitmap Heap Scan on quakes_earthquake  (cost=4.58..139.08 rows=38 width=140) (actual time=0.017..0.031 rows=21 loops=1)
              Recheck Cond: ((mag >= '6'::double precision) AND (NOT is_deleted))
              Heap Blocks: exact=21
              Buffers: shared hit=23
              ->  Bitmap Index Scan on quake_visible_mag_time_idx  (cost=0.00..4.57 rows=38 width=0) (actual time=0.013..0.013 rows=25 loops=1)
                    Index Cond: (mag >= '6'::double precision)
                    Buffers: shared hit=2
Planning:
  Buffers: shared hit=8
Planning Time: 0.084 ms
Execution Time: 0.057 ms
```

## Common filter: min_mag=2.5, last 30 days

A typical dashboard query.

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE (NOT "quakes_earthquake"."is_deleted" AND "quakes_earthquake"."time" >= '2026-10-18 09:21:42.876591+00:00'::timestamptz AND "quakes_earthquake"."mag" >= 2.5) ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51
```

```
Limit  (cost=0.29..101.65 rows=51 width=140) (actual time=0.010..0.058 rows=51 loops=1)
  Buffers: shared hit=169
  ->  Index Scan using quake_visible_time_id_idx on quakes_earthquake  (cost=0.29..4064.89 rows=2045 width=140) (actual time=0.009..0.056 rows=51 loops=1)
        Index Cond: ("time" >= '2026-10-18 09:21:42.876591+00'::timestamp with time zone)
        Filter: (mag >= '2.5'::double precision)
        Rows Removed by Filter: 245
        Buffers: shared hit=169
Planning:
  Buffers: shared hit=8
Planning Time: 0.045 ms
Execution Time: 0.066 ms
```

## Bounding box: Japan region

bbox=128,30,146,46 (about 0.5% of the globe).

```sql
SELECT "quakes_earthquake"."id", "quakes_earthquake"."usgs_id", "quakes_earthquake"."time", "quakes_earthquake"."updated", "quakes_earthquake"."mag", "quakes_earthquake"."mag_type", "quakes_earthquake"."latitude", "quakes_earthquake"."longitude", "quakes_earthquake"."depth_km", "quakes_earthquake"."place", "quakes_earthquake"."status", "quakes_earthquake"."event_type", "quakes_earthquake"."tsunami", "quakes_earthquake"."sig", "quakes_earthquake"."is_deleted", "quakes_earthquake"."created_at", "quakes_earthquake"."last_ingested_at" FROM "quakes_earthquake" WHERE (NOT "quakes_earthquake"."is_deleted" AND "quakes_earthquake"."latitude" >= 30.0 AND "quakes_earthquake"."latitude" <= 46.0 AND "quakes_earthquake"."longitude" >= 128.0 AND "quakes_earthquake"."longitude" <= 146.0) ORDER BY "quakes_earthquake"."time" DESC, "quakes_earthquake"."id" DESC LIMIT 51
```

```
Limit  (cost=396.22..396.35 rows=51 width=140) (actual time=0.188..0.191 rows=51 loops=1)
  Buffers: shared hit=81
  ->  Sort  (cost=396.22..396.39 rows=66 width=140) (actual time=0.187..0.188 rows=51 loops=1)
        Sort Key: "time" DESC, id DESC
        Sort Method: quicksort  Memory: 38kB
        Buffers: shared hit=81
        ->  Bitmap Heap Scan on quakes_earthquake  (cost=173.13..394.23 rows=66 width=140) (actual time=0.142..0.175 rows=53 loops=1)
              Recheck Cond: ((latitude >= '30'::double precision) AND (latitude <= '46'::double precision) AND (longitude >= '128'::double precision) AND (longitude <= '146'::double precision))
              Filter: (NOT is_deleted)
              Heap Blocks: exact=56
              Buffers: shared hit=81
              ->  Bitmap Index Scan on quake_lat_lon_idx  (cost=0.00..173.11 rows=66 width=0) (actual time=0.135..0.135 rows=58 loops=1)
                    Index Cond: ((latitude >= '30'::double precision) AND (latitude <= '46'::double precision) AND (longitude >= '128'::double precision) AND (longitude <= '146'::double precision))
                    Buffers: shared hit=25
Planning:
  Buffers: shared hit=3
Planning Time: 0.054 ms
Execution Time: 0.219 ms
```

## Summary

| Query | Variant | Access path | Execution time |
|---|---|---|---|
| Default list, first page | with index | Index Scan using quake_visible_time_id_idx | 0.08 ms |
| Deep page via cursor (~10,496 rows in) | with index | Index Scan using quake_visible_time_id_idx | 0.04 ms |
| Deep page via OFFSET (10,496) | with index | Seq Scan | 12.78 ms |
| Rare filter: min_mag=6 | with index | Bitmap Heap Scan | 0.06 ms |
| Common filter: min_mag=2.5, last 30 days | with index | Index Scan using quake_visible_time_id_idx | 0.07 ms |
| Bounding box: Japan region | with index | Bitmap Heap Scan | 0.22 ms |