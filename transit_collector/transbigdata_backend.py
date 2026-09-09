from __future__ import annotations

import contextlib
import hashlib
import io
import json
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Iterable

import geopandas as gpd
import pandas as pd
import requests
from shapely.geometry import LineString, MultiLineString

from transit_collector.db import TransitDB


BAIDU_SEARCH_URL = "https://map.baidu.com/"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://map.baidu.com/",
}


def normalize_line_keyword(value: str) -> str:
    """Turn a Baidu direction name such as '1路(A-B)' into the searchable base line name."""
    text = str(value or "").strip()
    text = re.sub(r"\s+", "", text)
    if "(" in text:
        text = text.split("(", 1)[0]
    if "（" in text:
        text = text.split("（", 1)[0]
    return text.strip()


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    return prefix + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def _split_direction(linename: str) -> tuple[str, str]:
    match = re.search(r"[（(]([^()（）]+?)[-—–~～至]([^()（）]+?)[)）]\s*$", linename or "")
    if not match:
        return "", ""
    return match.group(1).strip(), match.group(2).strip()


def _line_coords(geometry) -> list[tuple[float, float]]:
    if geometry is None or getattr(geometry, "is_empty", True):
        return []
    if isinstance(geometry, LineString):
        return [(float(x), float(y)) for x, y in geometry.coords]
    if isinstance(geometry, MultiLineString):
        parts = sorted(geometry.geoms, key=lambda g: g.length, reverse=True)
        if parts:
            return [(float(x), float(y)) for x, y in parts[0].coords]
    return []


def _stop_id(city: str, name: str, lon: float, lat: float) -> str:
    # 1e-6 degree is roughly decimetre scale in China. Nearby direction-specific
    # points may still get separate raw IDs and are subsequently merged spatially.
    return _stable_id("TBDSTOP_", city, name, f"{lon:.6f}", f"{lat:.6f}")


@dataclass
class DiscoveryConfig:
    mode: str = "full"
    numeric_max: int = 999
    prefix_max: int = 199
    prefixes: tuple[str, ...] = ("K", "B", "Y", "夜", "游", "快")
    broad_seeds: tuple[str, ...] = (
        "公交",
        "公交线路",
        "专线",
        "城际",
        "机场",
        "环线",
        "快线",
        "夜班",
        "旅游",
        "高铁",
    )
    extra_keywords: tuple[str, ...] = ()
    timeout: float = 15.0
    workers: int = 4
    broad_pages: int = 6
    batch_size: int = 12
    delay_min: float = 0.08
    delay_max: float = 0.22

    @classmethod
    def quick(cls, extra_keywords: Iterable[str] = ()) -> "DiscoveryConfig":
        return cls(
            mode="quick",
            numeric_max=300,
            prefix_max=99,
            workers=4,
            broad_pages=4,
            extra_keywords=tuple(extra_keywords),
        )

    @classmethod
    def full(cls, extra_keywords: Iterable[str] = ()) -> "DiscoveryConfig":
        return cls(extra_keywords=tuple(extra_keywords))

    def probe_keywords(self) -> list[str]:
        values = [f"{n}路" for n in range(1, self.numeric_max + 1)]
        for prefix in self.prefixes:
            values.extend(f"{prefix}{n}路" for n in range(1, self.prefix_max + 1))
        values.extend(k.strip() for k in self.extra_keywords if str(k).strip())
        # Stable de-duplication makes resume state deterministic.
        return list(dict.fromkeys(values))


class _LogWriter(io.TextIOBase):
    def __init__(self, callback: Callable[[str], None]):
        self.callback = callback
        self._buf = ""

    def write(self, text: str) -> int:
        self._buf += str(text)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.callback(line.strip())
        return len(text)

    def flush(self):
        if self._buf.strip():
            self.callback(self._buf.strip())
        self._buf = ""


class BaiduLineDiscoverer:
    """Discover bus-line keywords from the same Baidu web search used by TransBigData.

    The old project depended on 8684.cn for a city-wide line-name directory. This
    class removes that dependency. It combines paged broad searches with an exact
    numeric/prefix sweep and persists probe state in SQLite for resumable scans.
    """

    def __init__(
        self,
        city: str,
        db: TransitDB,
        config: DiscoveryConfig,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
    ):
        self.city = city.strip()
        self.db = db
        self.config = config
        self.cancel_event = cancel_event or threading.Event()
        self.log = log or (lambda _s: None)
        self.progress = progress or (lambda _stage, _cur, _total: None)
        self.city_code = ""
        self.request_count = 0
        self._ensure_schema()

    def _ensure_schema(self):
        self.db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tbd_probe (
                keyword TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                route_names TEXT,
                last_error TEXT
            );
            CREATE TABLE IF NOT EXISTS tbd_line_keywords (
                line_name TEXT PRIMARY KEY,
                source TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
            );
            """
        )
        self.db.conn.commit()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise InterruptedError("用户已停止 TransBigData 采集")

    def _request(self, params: dict) -> dict:
        self._check_cancel()
        self.request_count += 1
        response = requests.get(BAIDU_SEARCH_URL, params=params, headers=HEADERS, timeout=self.config.timeout)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise RuntimeError("百度地图搜索未返回 JSON，可能触发了风控或接口结构已变化。") from exc

    def get_city_code(self) -> str:
        data = self._request({"qt": "s", "wd": self.city, "from": "webmap"})
        content = data.get("content")
        code = ""
        if isinstance(content, dict):
            code = str(content.get("code", ""))
        if not code:
            result = data.get("result") or {}
            code = str(result.get("city_code", "") or result.get("code", ""))
        if not code:
            raise RuntimeError(f"TransBigData/百度地图无法识别城市：{self.city}")
        self.city_code = code
        self.db.set_meta("baidu_city_code", code)
        return code

    @staticmethod
    def _route_items(data: dict, exact: bool = False) -> list[dict]:
        content = data.get("content")
        if not isinstance(content, list):
            return []
        out = []
        for item in content:
            if not isinstance(item, dict):
                continue
            try:
                is_line = int(item.get("geo_type", -1)) == 1
            except (TypeError, ValueError):
                is_line = str(item.get("geo_type", "")) == "1"
            if not is_line:
                continue
            if exact:
                try:
                    if int(item.get("acc_flag", 0)) != 1:
                        continue
                except (TypeError, ValueError):
                    if str(item.get("acc_flag", "")) != "1":
                        continue
            out.append(item)
        return out

    def _search(self, keyword: str, page: int = 0) -> dict:
        params = {
            "qt": "s",
            "wd": keyword,
            "c": self.city_code,
            "from": "webmap",
            "pn": page,
            "nn": page * 10,
        }
        return self._request(params)

    def _add_discovered(self, names: Iterable[str], source: str):
        rows = []
        for name in names:
            base = normalize_line_keyword(name)
            if base:
                rows.append((base, source))
        if rows:
            self.db.conn.executemany(
                """
                INSERT INTO tbd_line_keywords(line_name,source,status)
                VALUES(?,?,'pending')
                ON CONFLICT(line_name) DO UPDATE SET source=COALESCE(tbd_line_keywords.source,excluded.source)
                """,
                rows,
            )
            self.db.conn.commit()

    def _broad_discovery(self):
        key = f"tbd_broad_done_{self.city_code}"
        if self.db.get_meta(key, "0") == "1":
            return
        found: set[str] = set()
        total = len(self.config.broad_seeds) * self.config.broad_pages
        cursor = 0
        self.log("阶段 1/3：用百度地图分页搜索补充命名线路（城际、专线、机场、环线等）…")
        for seed in self.config.broad_seeds:
            empty_pages = 0
            for page in range(self.config.broad_pages):
                self._check_cancel()
                cursor += 1
                self.progress(f"{self.city} · 命名线路发现", cursor, max(total, 1))
                try:
                    data = self._search(seed, page)
                    items = self._route_items(data, exact=False)
                except Exception as exc:
                    self.log(f"宽泛搜索失败：{seed} 第{page + 1}页 -> {exc}")
                    break
                names = {normalize_line_keyword(x.get("name", "")) for x in items}
                names.discard("")
                if not names:
                    empty_pages += 1
                    if empty_pages >= 2:
                        break
                else:
                    empty_pages = 0
                    found.update(names)
                content = data.get("content")
                if not isinstance(content, list) or not content:
                    break
        self._add_discovered(found, "baidu-broad")
        self.db.set_meta(key, "1")
        self.log(f"命名线路搜索累计发现 {len(found)} 个线路关键词。")

    def _probe_one(self, keyword: str) -> tuple[str, list[str], str]:
        if self.cancel_event.is_set():
            return keyword, [], "cancelled"
        if self.config.delay_max > 0:
            time.sleep(random.uniform(self.config.delay_min, self.config.delay_max))
        try:
            data = self._search(keyword, 0)
            items = self._route_items(data, exact=True)
            names = [normalize_line_keyword(x.get("name", "")) for x in items]
            names = [x for x in names if x]
            if items and keyword not in names:
                names.append(keyword)
            return keyword, list(dict.fromkeys(names)), ""
        except Exception as exc:
            return keyword, [], str(exc)

    def _exact_probe(self):
        candidates = self.config.probe_keywords()
        self.db.conn.executemany(
            "INSERT OR IGNORE INTO tbd_probe(keyword,status) VALUES(?,'pending')",
            [(x,) for x in candidates],
        )
        self.db.conn.commit()
        rows = self.db.conn.execute(
            "SELECT keyword FROM tbd_probe WHERE status='pending' ORDER BY keyword"
        ).fetchall()
        pending = [r[0] for r in rows]
        if not pending:
            return

        self.log(
            f"阶段 2/3：线路名扫描 {len(pending)} 个待探测关键词；"
            f"模式={self.config.mode}，并发={self.config.workers}。"
        )
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as pool:
            future_map = {pool.submit(self._probe_one, keyword): keyword for keyword in pending}
            for future in as_completed(future_map):
                self._check_cancel()
                keyword, names, error = future.result()
                done += 1
                if error == "cancelled":
                    continue
                status = "done" if not error else "error"
                self.db.conn.execute(
                    "UPDATE tbd_probe SET status=?,route_names=?,last_error=? WHERE keyword=?",
                    (status, json.dumps(names, ensure_ascii=False), error, keyword),
                )
                if names:
                    self._add_discovered(names, "baidu-exact-scan")
                if done % 25 == 0 or done == len(pending):
                    self.db.conn.commit()
                    count = self.db.conn.execute("SELECT COUNT(*) FROM tbd_line_keywords").fetchone()[0]
                    self.progress(f"{self.city} · 扫描线路名（已发现{count}）", done, len(pending))
        self.db.conn.commit()

    def discover(self) -> tuple[str, list[str]]:
        if not self.city:
            raise ValueError("城市名不能为空。")
        self.get_city_code()
        self._broad_discovery()
        self._exact_probe()
        rows = self.db.conn.execute(
            "SELECT line_name FROM tbd_line_keywords ORDER BY line_name"
        ).fetchall()
        names = [r[0] for r in rows]
        if not names:
            raise RuntimeError(
                "没有发现公交线路。请检查城市名称，或在“额外线路关键词”中补充当地特殊线路名称。"
            )
        return self.city_code, names


class TransBigDataCollector:
    def __init__(
        self,
        city: str,
        db: TransitDB,
        config: DiscoveryConfig | None = None,
        merge_radius_m: float = 120.0,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        metrics: Callable[[dict], None] | None = None,
    ):
        self.city = city.strip()
        self.db = db
        self.config = config or DiscoveryConfig.full()
        self.merge_radius_m = merge_radius_m
        self.cancel_event = cancel_event or threading.Event()
        self.log = log or (lambda _s: None)
        self.progress = progress or (lambda _stage, _cur, _total: None)
        self.metrics = metrics or (lambda _m: None)
        self.discovery_requests = 0
        self._ensure_schema()

    def _ensure_schema(self):
        self.db.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tbd_fetch_state (
                line_name TEXT PRIMARY KEY,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT
            )
            """
        )
        self.db.conn.commit()

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise InterruptedError("用户已停止 TransBigData 采集")

    def _emit_metrics(self):
        m = self.db.counts()
        m["api_calls"] = self.discovery_requests
        try:
            m["pois"] = self.db.conn.execute("SELECT COUNT(*) FROM tbd_line_keywords").fetchone()[0]
        except Exception:
            m["pois"] = 0
        self.metrics(m)

    def run(self):
        self.db.set_meta("scope", {"name": self.city, "level": "city", "source": "TransBigData/Baidu"})
        self.db.set_meta("data_source", "TransBigData/Baidu")
        self.db.set_meta("coordinate_system", "WGS84")
        self.db.set_meta("completed", "0")

        discoverer = BaiduLineDiscoverer(
            city=self.city,
            db=self.db,
            config=self.config,
            cancel_event=self.cancel_event,
            log=self.log,
            progress=self.progress,
        )
        city_code, line_names = discoverer.discover()
        self.discovery_requests = discoverer.request_count
        self._emit_metrics()
        self.log(f"共发现 {len(line_names)} 个公交线路关键词，开始交给 TransBigData 获取线路和站点。")

        self.db.conn.executemany(
            "INSERT OR IGNORE INTO tbd_fetch_state(line_name,status) VALUES(?,'pending')",
            [(x,) for x in line_names],
        )
        self.db.conn.commit()
        pending_rows = self.db.conn.execute(
            "SELECT line_name FROM tbd_fetch_state WHERE status<>'done' ORDER BY line_name"
        ).fetchall()
        pending = [r[0] for r in pending_rows]

        try:
            import transbigdata as tbd
        except ImportError as exc:
            raise RuntimeError("缺少 transbigdata，请重新运行 requirements.txt 安装依赖。") from exc

        total = len(pending)
        self.log(f"阶段 3/3：TransBigData 待抓取 {total} 个线路关键词。")
        batch_size = max(1, int(self.config.batch_size))
        for start in range(0, total, batch_size):
            self._check_cancel()
            batch = pending[start:start + batch_size]
            current = min(start + len(batch), total)
            self.progress(f"{self.city} · TransBigData 获取线路与站点", current, max(total, 1))
            writer = _LogWriter(self.log)
            try:
                with contextlib.redirect_stdout(writer):
                    line_gdf, stop_gdf = tbd.getbusdata(
                        city=self.city,
                        keywords=batch,
                        accurate=True,
                        timeout=self.config.timeout,
                    )
                writer.flush()
                ingest_transbigdata_frames(self.db, self.city, city_code, line_gdf, stop_gdf)
                self.db.conn.executemany(
                    "UPDATE tbd_fetch_state SET status='done',last_error='' WHERE line_name=?",
                    [(x,) for x in batch],
                )
                self.db.conn.commit()
            except Exception as exc:
                writer.flush()
                self.db.conn.executemany(
                    "UPDATE tbd_fetch_state SET status='error',last_error=? WHERE line_name=?",
                    [(str(exc), x) for x in batch],
                )
                self.db.conn.commit()
                self.log(f"TransBigData 批次失败：{', '.join(batch)} -> {exc}")
            self._emit_metrics()

        self._check_cancel()
        self.log("正在按站名 + 空间距离合并物理公交站点…")
        self.db.rebuild_station_groups(self.merge_radius_m)
        self.db.set_meta("completed", "1")
        self._emit_metrics()
        self.log("TransBigData 采集完成，可导出 CSV / Excel / GPKG / SHP 并继续 GIS 分析。")


def ingest_transbigdata_frames(
    db: TransitDB,
    city: str,
    city_code: str,
    line_gdf: gpd.GeoDataFrame | pd.DataFrame,
    stop_gdf: gpd.GeoDataFrame | pd.DataFrame,
) -> int:
    """Adapt TransBigData getbusdata output to the existing relational database."""
    if line_gdf is None or len(line_gdf) == 0:
        return 0
    stops = stop_gdf.copy() if stop_gdf is not None else pd.DataFrame()
    inserted = 0

    for _, row in line_gdf.iterrows():
        linename = str(row.get("linename", "") or "").strip()
        if not linename:
            continue
        coords = _line_coords(row.get("geometry"))
        if len(coords) < 2:
            continue

        line_stops = stops[stops.get("linename", pd.Series(index=stops.index, dtype=str)) == linename].copy()
        if not line_stops.empty:
            if "id" in line_stops.columns:
                line_stops["_seq"] = pd.to_numeric(line_stops["id"], errors="coerce")
                line_stops = line_stops.sort_values("_seq", kind="stable")
            else:
                line_stops["_seq"] = range(1, len(line_stops) + 1)

        busstops = []
        for seq_fallback, (_, srow) in enumerate(line_stops.iterrows(), start=1):
            name = str(srow.get("stationnames", "") or "").strip()
            try:
                lon = float(srow.get("lon"))
                lat = float(srow.get("lat"))
            except (TypeError, ValueError):
                geom = srow.get("geometry")
                if geom is None or getattr(geom, "is_empty", True):
                    continue
                lon, lat = float(geom.x), float(geom.y)
            try:
                seq = int(float(srow.get("_seq", seq_fallback)))
            except (TypeError, ValueError):
                seq = seq_fallback
            sid = _stop_id(city, name, lon, lat)
            busstops.append(
                {
                    "id": sid,
                    "name": name,
                    "location": f"{lon:.8f},{lat:.8f}",
                    "sequence": seq,
                    "citycode": str(city_code),
                }
            )

        start_stop, end_stop = _split_direction(linename)
        if busstops:
            start_stop = start_stop or busstops[0]["name"]
            end_stop = end_stop or busstops[-1]["name"]

        line_id = _stable_id("TBDLINE_", city, linename)
        polyline = ";".join(f"{lon:.8f},{lat:.8f}" for lon, lat in coords)
        db.upsert_line(
            {
                "id": line_id,
                "name": linename,
                "type": "bus",
                "citycode": str(city_code),
                "start_stop": start_stop,
                "end_stop": end_stop,
                "direc": f"{start_stop}->{end_stop}" if start_stop or end_stop else "",
                "polyline": polyline,
                "busstops": busstops,
            },
            f"BD{city_code}",
        )
        inserted += 1

    db.set_meta("coordinate_system", "WGS84")
    db.set_meta("data_source", "TransBigData/Baidu")
    db.commit()
    return inserted
