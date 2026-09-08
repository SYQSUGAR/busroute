from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Callable

from transit_collector.api.amap import AMapClient, AMapError, District
from transit_collector.db import TransitDB
from transit_collector.utils import (
    bbox_from_polyline,
    ensure_list,
    normalize_station_name,
    rect_key,
    rect_size_deg,
    split_rect,
)


MUNICIPALITIES = {"北京市", "上海市", "天津市", "重庆市"}


@dataclass
class Scope:
    name: str
    adcode: str
    level: str
    citycode: str = ""


class TransitCrawler:
    def __init__(
        self,
        client: AMapClient,
        db: TransitDB,
        scope: Scope,
        merge_radius_m: float = 120.0,
        cancel_event: threading.Event | None = None,
        log: Callable[[str], None] | None = None,
        progress: Callable[[str, int, int], None] | None = None,
        metrics: Callable[[], None] | None = None,
    ):
        self.client = client
        self.db = db
        self.scope = scope
        self.merge_radius_m = merge_radius_m
        self.cancel_event = cancel_event or threading.Event()
        self.log = log or (lambda s: None)
        self.progress = progress or (lambda stage, cur, total: None)
        self.metrics = metrics or (lambda: None)

    def _check_cancel(self):
        if self.cancel_event.is_set():
            raise InterruptedError("用户已停止采集")

    def run(self):
        self.db.set_meta("scope", {
            "name": self.scope.name,
            "adcode": self.scope.adcode,
            "level": self.scope.level,
        })
        city_units = self._resolve_city_units()
        self.log(f"采集范围：{self.scope.name}，共 {len(city_units)} 个城市/采集单元")

        for idx, city in enumerate(city_units, start=1):
            self._check_cancel()
            self.log(f"[{idx}/{len(city_units)}] 开始：{city.name} ({city.adcode})")
            self._crawl_city(city)

        self._check_cancel()
        self.log("正在合并同名近邻站点…")
        self.db.rebuild_station_groups(self.merge_radius_m)
        self.metrics()
        self.db.set_meta("completed", "1")
        self.log("采集完成。可导出 CSV / Excel / GeoJSON。")

    def _resolve_city_units(self) -> list[Scope]:
        if self.scope.level == "city":
            return [self.scope]
        if self.scope.level != "province":
            raise ValueError(f"不支持的范围级别：{self.scope.level}")

        if self.scope.name in MUNICIPALITIES:
            return [Scope(self.scope.name, self.scope.adcode, "city", self.scope.citycode)]

        district = self.client.get_district(self.scope.adcode, subdistrict=1, extensions="base")
        children = ensure_list(district.districts)
        units = []
        for child in children:
            level = str(child.get("level", ""))
            # Normal prefecture-level cities + province-direct county-level units.
            if level in {"city", "district"}:
                units.append(
                    Scope(
                        name=str(child.get("name", "")),
                        adcode=str(child.get("adcode", "")),
                        level="city",
                        citycode=str(child.get("citycode", "") if not isinstance(child.get("citycode"), list) else ""),
                    )
                )
        if not units:
            # Fallback to province itself as a query unit.
            units = [Scope(self.scope.name, self.scope.adcode, "city", self.scope.citycode)]
        return units

    def _crawl_city(self, city: Scope):
        detail = self.client.get_district(city.adcode, subdistrict=1, extensions="all")
        allowed_adcodes = {city.adcode}
        for child in ensure_list(detail.districts):
            ad = str(child.get("adcode", ""))
            if ad:
                allowed_adcodes.add(ad)

        bbox = bbox_from_polyline(detail.polyline)
        self.log(f"{city.name} 边界外包框：{bbox[0]:.4f},{bbox[1]:.4f} ~ {bbox[2]:.4f},{bbox[3]:.4f}")

        # Stage 1: POI spatial discovery
        self._discover_pois(city, bbox, allowed_adcodes)
        self.metrics()

        # Stage 2: stop -> line IDs
        names = self.db.pending_poi_names(allowed_adcodes)
        total = len(names)
        self.log(f"{city.name}：待查询公交站名 {total} 个")
        for i, query_name in enumerate(names, start=1):
            self._check_cancel()
            self.progress(f"{city.name} · 查询站点", i, max(total, 1))
            try:
                results = self.client.search_bus_stops(query_name, city.adcode, page=1, offset=100)
            except AMapError as e:
                if e.is_quota_error:
                    raise
                self.log(f"站点查询失败：{query_name} -> {e}")
                continue

            inserted = 0
            for stop in results:
                stop_adcode = str(stop.get("adcode", ""))
                if allowed_adcodes and stop_adcode and stop_adcode not in allowed_adcodes:
                    continue
                if normalize_station_name(str(stop.get("name", ""))) != query_name:
                    # Keep fuzzy results out to avoid cross-station contamination.
                    continue
                self.db.upsert_raw_stop(stop, "stopname")
                sid = str(stop.get("id", ""))
                for line in ensure_list(stop.get("buslines")):
                    self.db.add_raw_stop_line(sid, line, "stopname")
                inserted += 1
            self.db.mark_poi_name_enriched(query_name, allowed_adcodes)
            self.db.commit()
            if i % 20 == 0 or i == total:
                self.metrics()
            if inserted == 0:
                self.log(f"未匹配到公交站 API 记录：{query_name}")

        # Stage 3: line details
        line_ids = self.db.pending_line_ids(allowed_adcodes)
        total_lines = len(line_ids)
        self.log(f"{city.name}：待获取线路详情 {total_lines} 条")
        for i, line_id in enumerate(line_ids, start=1):
            self._check_cancel()
            self.progress(f"{city.name} · 获取线路详情", i, max(total_lines, 1))
            try:
                lines = self.client.get_bus_line(line_id)
            except AMapError as e:
                if e.is_quota_error:
                    raise
                self.log(f"线路详情失败：{line_id} -> {e}")
                continue
            if not lines:
                self.log(f"线路 ID 无返回：{line_id}")
                continue
            for line in lines:
                self.db.upsert_line(line, city.adcode)
            if i % 20 == 0 or i == total_lines:
                self.metrics()

    def _discover_pois(self, city: Scope, bbox, allowed_adcodes: set[str]):
        stack = [(bbox, 0)]
        processed = 0
        while stack:
            self._check_cancel()
            rect, depth = stack.pop()
            key = rect_key(city.adcode, rect, depth)
            status = self.db.tile_status(key)
            if status and status["status"] in {"done", "subdivided"}:
                continue

            self.progress(f"{city.name} · 空间发现公交站", processed + 1, processed + len(stack) + 1)
            first = self.client.search_bus_stop_pois_rect(rect, page_num=1, page_size=25)
            try:
                count = int(first.get("count") or 0)
            except (TypeError, ValueError):
                count = len(ensure_list(first.get("pois")))

            # Search API caps the same query at 200 results, so subdivide dense cells.
            if count >= 180 and depth < 8 and rect_size_deg(rect) > 0.01:
                self.db.mark_tile(key, city.adcode, depth, rect, "subdivided", count)
                stack.extend((sub, depth + 1) for sub in split_rect(rect))
                self.log(f"{city.name}：高密度网格 {count} 条，自动细分（深度 {depth + 1}）")
                processed += 1
                continue

            pages = max(1, math.ceil(min(count, 200) / 25))
            pages_data = [first]
            for page in range(2, pages + 1):
                self._check_cancel()
                pages_data.append(self.client.search_bus_stop_pois_rect(rect, page_num=page, page_size=25))

            accepted = 0
            for page_data in pages_data:
                for poi in ensure_list(page_data.get("pois")):
                    adcode = str(poi.get("adcode", ""))
                    if adcode and allowed_adcodes and adcode not in allowed_adcodes:
                        continue
                    self.db.upsert_poi(poi)
                    accepted += 1
            self.db.commit()
            self.db.mark_tile(key, city.adcode, depth, rect, "done", count)
            processed += 1
            if accepted:
                self.log(f"{city.name}：网格发现 {accepted} 条公交站 POI")
            if processed % 5 == 0:
                self.metrics()
