from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable

from transit_collector.utils import normalize_station_name, parse_location, haversine_m, stable_group_id


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS tiles (
    tile_key TEXT PRIMARY KEY,
    city_adcode TEXT NOT NULL,
    depth INTEGER NOT NULL,
    minx REAL NOT NULL,
    miny REAL NOT NULL,
    maxx REAL NOT NULL,
    maxy REAL NOT NULL,
    status TEXT NOT NULL,
    result_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS poi_stops (
    poi_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    query_name TEXT NOT NULL,
    longitude REAL,
    latitude REAL,
    adcode TEXT,
    cityname TEXT,
    address TEXT,
    enriched INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS raw_stops (
    stop_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    norm_name TEXT NOT NULL,
    longitude REAL,
    latitude REAL,
    adcode TEXT,
    citycode TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS raw_stop_lines (
    stop_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    line_name TEXT,
    start_stop TEXT,
    end_stop TEXT,
    source TEXT,
    PRIMARY KEY (stop_id, line_id),
    FOREIGN KEY(stop_id) REFERENCES raw_stops(stop_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS bus_lines (
    line_id TEXT PRIMARY KEY,
    name TEXT,
    type TEXT,
    citycode TEXT,
    crawl_city_adcode TEXT,
    start_stop TEXT,
    end_stop TEXT,
    start_time TEXT,
    end_time TEXT,
    timedesc TEXT,
    distance REAL,
    loop TEXT,
    status TEXT,
    direc TEXT,
    company TEXT,
    basic_price TEXT,
    total_price TEXT,
    bounds TEXT,
    polyline TEXT,
    fetched INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS line_stops (
    line_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    stop_id TEXT NOT NULL,
    stop_name TEXT,
    longitude REAL,
    latitude REAL,
    PRIMARY KEY (line_id, sequence),
    FOREIGN KEY(line_id) REFERENCES bus_lines(line_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS station_groups (
    station_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    longitude REAL,
    latitude REAL,
    member_count INTEGER DEFAULT 0,
    line_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS station_members (
    station_id TEXT NOT NULL,
    stop_id TEXT NOT NULL UNIQUE,
    PRIMARY KEY (station_id, stop_id)
);

CREATE TABLE IF NOT EXISTS station_lines (
    station_id TEXT NOT NULL,
    line_id TEXT NOT NULL,
    PRIMARY KEY (station_id, line_id)
);

CREATE INDEX IF NOT EXISTS idx_poi_query_name ON poi_stops(query_name);
CREATE INDEX IF NOT EXISTS idx_raw_stop_norm_name ON raw_stops(norm_name);
CREATE INDEX IF NOT EXISTS idx_line_stops_stop_id ON line_stops(stop_id);
"""


class TransitDB:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self):
        self.conn.commit()
        self.conn.close()

    def set_meta(self, key: str, value):
        if not isinstance(value, str):
            value = json.dumps(value, ensure_ascii=False)
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def tile_status(self, tile_key: str):
        row = self.conn.execute("SELECT status,result_count FROM tiles WHERE tile_key=?", (tile_key,)).fetchone()
        return dict(row) if row else None

    def mark_tile(self, tile_key: str, city_adcode: str, depth: int, rect, status: str, result_count: int = 0):
        self.conn.execute(
            """
            INSERT INTO tiles(tile_key,city_adcode,depth,minx,miny,maxx,maxy,status,result_count)
            VALUES(?,?,?,?,?,?,?,?,?)
            ON CONFLICT(tile_key) DO UPDATE SET status=excluded.status,result_count=excluded.result_count
            """,
            (tile_key, city_adcode, depth, *rect, status, result_count),
        )
        self.conn.commit()

    def upsert_poi(self, poi: dict):
        lon, lat = parse_location(poi.get("location"))
        name = str(poi.get("name", "")).strip()
        poi_id = str(poi.get("id", "")).strip()
        if not poi_id or not name:
            return
        self.conn.execute(
            """
            INSERT INTO poi_stops(poi_id,name,query_name,longitude,latitude,adcode,cityname,address)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(poi_id) DO UPDATE SET
              name=excluded.name, query_name=excluded.query_name, longitude=excluded.longitude,
              latitude=excluded.latitude, adcode=excluded.adcode, cityname=excluded.cityname,
              address=excluded.address
            """,
            (
                poi_id,
                name,
                normalize_station_name(name),
                lon,
                lat,
                str(poi.get("adcode", "")),
                str(poi.get("cityname", "")),
                str(poi.get("address", "")),
            ),
        )

    def pending_poi_names(self, city_allowed_adcodes: set[str] | None = None) -> list[str]:
        if city_allowed_adcodes:
            placeholders = ",".join("?" for _ in city_allowed_adcodes)
            sql = f"""
                SELECT DISTINCT query_name FROM poi_stops
                WHERE enriched=0 AND adcode IN ({placeholders}) AND query_name<>''
                ORDER BY query_name
            """
            rows = self.conn.execute(sql, tuple(city_allowed_adcodes)).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT DISTINCT query_name FROM poi_stops WHERE enriched=0 AND query_name<>'' ORDER BY query_name"
            ).fetchall()
        return [r[0] for r in rows]

    def mark_poi_name_enriched(self, query_name: str, city_allowed_adcodes: set[str] | None = None):
        if city_allowed_adcodes:
            placeholders = ",".join("?" for _ in city_allowed_adcodes)
            self.conn.execute(
                f"UPDATE poi_stops SET enriched=1 WHERE query_name=? AND adcode IN ({placeholders})",
                (query_name, *tuple(city_allowed_adcodes)),
            )
        else:
            self.conn.execute("UPDATE poi_stops SET enriched=1 WHERE query_name=?", (query_name,))
        self.conn.commit()

    def upsert_raw_stop(self, stop: dict, source: str):
        stop_id = str(stop.get("id", "")).strip()
        name = str(stop.get("name", "")).strip()
        if not stop_id or not name:
            return
        lon, lat = parse_location(stop.get("location"))
        self.conn.execute(
            """
            INSERT INTO raw_stops(stop_id,name,norm_name,longitude,latitude,adcode,citycode,source)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(stop_id) DO UPDATE SET
              name=excluded.name, norm_name=excluded.norm_name,
              longitude=COALESCE(excluded.longitude,raw_stops.longitude),
              latitude=COALESCE(excluded.latitude,raw_stops.latitude),
              adcode=CASE WHEN excluded.adcode<>'' THEN excluded.adcode ELSE raw_stops.adcode END,
              citycode=CASE WHEN excluded.citycode<>'' THEN excluded.citycode ELSE raw_stops.citycode END
            """,
            (
                stop_id, name, normalize_station_name(name), lon, lat,
                str(stop.get("adcode", "")), str(stop.get("citycode", "")), source,
            ),
        )

    def add_raw_stop_line(self, stop_id: str, line: dict, source: str):
        line_id = str(line.get("id", "")).strip()
        if not stop_id or not line_id:
            return
        self.conn.execute(
            """
            INSERT INTO raw_stop_lines(stop_id,line_id,line_name,start_stop,end_stop,source)
            VALUES(?,?,?,?,?,?)
            ON CONFLICT(stop_id,line_id) DO UPDATE SET
              line_name=excluded.line_name,start_stop=excluded.start_stop,end_stop=excluded.end_stop
            """,
            (
                stop_id, line_id, str(line.get("name", "")),
                str(line.get("start_stop", "")), str(line.get("end_stop", "")), source,
            ),
        )

    def pending_line_ids(self, city_allowed_adcodes: set[str] | None = None) -> list[str]:
        if city_allowed_adcodes:
            placeholders = ",".join("?" for _ in city_allowed_adcodes)
            rows = self.conn.execute(
                f"""
                SELECT DISTINCT rsl.line_id
                FROM raw_stop_lines rsl
                JOIN raw_stops rs ON rs.stop_id=rsl.stop_id
                LEFT JOIN bus_lines bl ON bl.line_id=rsl.line_id
                WHERE (bl.line_id IS NULL OR bl.fetched<>1)
                  AND rs.adcode IN ({placeholders})
                ORDER BY rsl.line_id
                """,
                tuple(city_allowed_adcodes),
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT DISTINCT rsl.line_id
                FROM raw_stop_lines rsl
                LEFT JOIN bus_lines bl ON bl.line_id=rsl.line_id
                WHERE bl.line_id IS NULL OR bl.fetched<>1
                ORDER BY rsl.line_id
                """
            ).fetchall()
        return [r[0] for r in rows]

    def upsert_line(self, line: dict, crawl_city_adcode: str):
        line_id = str(line.get("id", "")).strip()
        if not line_id:
            return
        def val(k):
            v = line.get(k, "")
            if isinstance(v, (dict, list)):
                return json.dumps(v, ensure_ascii=False)
            return str(v if v is not None else "")
        try:
            distance = float(line.get("distance")) if line.get("distance") not in (None, "", []) else None
        except (TypeError, ValueError):
            distance = None
        self.conn.execute(
            """
            INSERT INTO bus_lines(
              line_id,name,type,citycode,crawl_city_adcode,start_stop,end_stop,start_time,end_time,
              timedesc,distance,loop,status,direc,company,basic_price,total_price,bounds,polyline,fetched
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(line_id) DO UPDATE SET
              name=excluded.name,type=excluded.type,citycode=excluded.citycode,
              crawl_city_adcode=excluded.crawl_city_adcode,start_stop=excluded.start_stop,
              end_stop=excluded.end_stop,start_time=excluded.start_time,end_time=excluded.end_time,
              timedesc=excluded.timedesc,distance=excluded.distance,loop=excluded.loop,
              status=excluded.status,direc=excluded.direc,company=excluded.company,
              basic_price=excluded.basic_price,total_price=excluded.total_price,bounds=excluded.bounds,
              polyline=excluded.polyline,fetched=1
            """,
            (
                line_id, val("name"), val("type"), val("citycode"), crawl_city_adcode,
                val("start_stop"), val("end_stop"), val("start_time"), val("end_time"),
                val("timedesc"), distance, val("loop"), val("status"), val("direc"),
                val("company"), val("basic_price"), val("total_price"), val("bounds"),
                val("polyline"),
            ),
        )
        self.conn.execute("DELETE FROM line_stops WHERE line_id=?", (line_id,))
        for idx, stop in enumerate(line.get("busstops") or [], start=1):
            self.upsert_raw_stop(stop, "lineid")
            stop_id = str(stop.get("id", "")).strip()
            seq = stop.get("sequence", idx)
            try:
                seq = int(seq)
            except (TypeError, ValueError):
                seq = idx
            lon, lat = parse_location(stop.get("location"))
            if stop_id:
                self.conn.execute(
                    """
                    INSERT OR REPLACE INTO line_stops(line_id,sequence,stop_id,stop_name,longitude,latitude)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (line_id, seq, stop_id, str(stop.get("name", "")), lon, lat),
                )
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO raw_stop_lines(stop_id,line_id,line_name,start_stop,end_stop,source)
                    VALUES(?,?,?,?,?,?)
                    """,
                    (
                        stop_id, line_id, val("name"), val("start_stop"), val("end_stop"), "lineid",
                    ),
                )
        self.conn.commit()

    def commit(self):
        self.conn.commit()

    def counts(self) -> dict[str, int]:
        return {
            "pois": self.conn.execute("SELECT COUNT(*) FROM poi_stops").fetchone()[0],
            "raw_stops": self.conn.execute("SELECT COUNT(*) FROM raw_stops").fetchone()[0],
            "lines": self.conn.execute("SELECT COUNT(*) FROM bus_lines").fetchone()[0],
            "stations": self.conn.execute("SELECT COUNT(*) FROM station_groups").fetchone()[0],
        }

    def rebuild_station_groups(self, radius_m: float = 120.0):
        rows = self.conn.execute(
            "SELECT stop_id,name,norm_name,longitude,latitude FROM raw_stops ORDER BY norm_name,stop_id"
        ).fetchall()
        by_name: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_name.setdefault(row["norm_name"] or row["name"], []).append(row)

        groups: list[tuple[str, str, float | None, float | None, list[str]]] = []

        for norm, items in by_name.items():
            # Union-find within same normalized station name.
            n = len(items)
            parent = list(range(n))

            def find(x):
                while parent[x] != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            def union(a, b):
                ra, rb = find(a), find(b)
                if ra != rb:
                    parent[rb] = ra

            for i in range(n):
                for j in range(i + 1, n):
                    a, b = items[i], items[j]
                    if None in (a["longitude"], a["latitude"], b["longitude"], b["latitude"]):
                        continue
                    if haversine_m(a["longitude"], a["latitude"], b["longitude"], b["latitude"]) <= radius_m:
                        union(i, j)

            clusters: dict[int, list[sqlite3.Row]] = {}
            for i, item in enumerate(items):
                clusters.setdefault(find(i), []).append(item)

            for cluster in clusters.values():
                ids = [r["stop_id"] for r in cluster]
                lons = [r["longitude"] for r in cluster if r["longitude"] is not None]
                lats = [r["latitude"] for r in cluster if r["latitude"] is not None]
                lon = sum(lons) / len(lons) if lons else None
                lat = sum(lats) / len(lats) if lats else None
                # Prefer the most common display name, then shortest.
                names = [r["name"] for r in cluster if r["name"]]
                display = min(names, key=lambda x: (-(names.count(x)), len(x), x)) if names else norm
                groups.append((stable_group_id(ids), display, lon, lat, ids))

        cur = self.conn.cursor()
        cur.execute("DELETE FROM station_lines")
        cur.execute("DELETE FROM station_members")
        cur.execute("DELETE FROM station_groups")

        for station_id, name, lon, lat, ids in groups:
            cur.execute(
                "INSERT INTO station_groups(station_id,name,longitude,latitude,member_count,line_count) VALUES(?,?,?,?,?,0)",
                (station_id, name, lon, lat, len(ids)),
            )
            cur.executemany(
                "INSERT INTO station_members(station_id,stop_id) VALUES(?,?)",
                [(station_id, sid) for sid in ids],
            )

        cur.execute(
            """
            INSERT OR IGNORE INTO station_lines(station_id,line_id)
            SELECT sm.station_id, ls.line_id
            FROM station_members sm
            JOIN line_stops ls ON ls.stop_id=sm.stop_id
            """
        )
        cur.execute(
            """
            UPDATE station_groups
            SET line_count=(SELECT COUNT(*) FROM station_lines sl WHERE sl.station_id=station_groups.station_id)
            """
        )
        self.conn.commit()
