from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Callable, Any

import requests

from transit_collector.utils import ensure_list


class AMapError(RuntimeError):
    def __init__(self, message: str, infocode: str = "", payload: dict | None = None):
        super().__init__(message)
        self.infocode = infocode
        self.payload = payload or {}

    @property
    def is_quota_error(self) -> bool:
        # AMap quota / QPS / access errors commonly fall in this family.
        return self.infocode in {
            "10003", "10004", "10005", "10020", "10021", "10044",
        }


@dataclass
class District:
    name: str
    adcode: str
    citycode: str
    level: str
    center: str = ""
    polyline: str = ""
    districts: list[dict] | None = None


class RateLimiter:
    def __init__(self, min_interval: float = 0.35):
        self.min_interval = max(0.0, float(min_interval))
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            delay = self.min_interval - (now - self._last)
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


class AMapClient:
    DISTRICT_URL = "https://restapi.amap.com/v3/config/district"
    POI_POLYGON_URL = "https://restapi.amap.com/v5/place/polygon"
    BUS_STOP_NAME_URL = "https://restapi.amap.com/v3/bus/stopname"
    BUS_LINE_ID_URL = "https://restapi.amap.com/v3/bus/lineid"
    BUS_LINE_NAME_URL = "https://restapi.amap.com/v3/bus/linename"

    def __init__(
        self,
        key: str,
        min_interval: float = 0.35,
        timeout: float = 20,
        max_retries: int = 3,
        on_request: Callable[[str], None] | None = None,
    ):
        self.key = key.strip()
        self.timeout = timeout
        self.max_retries = max_retries
        self.rate = RateLimiter(min_interval)
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "AMapTransitCollector/0.1"})
        self.on_request = on_request

    def _get(self, endpoint_name: str, url: str, params: dict[str, Any]) -> dict:
        params = dict(params)
        params["key"] = self.key
        params.setdefault("output", "json")

        last_error = None
        for attempt in range(self.max_retries + 1):
            self.rate.wait()
            if self.on_request:
                self.on_request(endpoint_name)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
                resp.raise_for_status()
                data = resp.json()
                if str(data.get("status", "1")) != "1":
                    code = str(data.get("infocode", ""))
                    msg = data.get("info") or data.get("infocode") or "高德 API 返回失败"
                    raise AMapError(f"{endpoint_name}: {msg} ({code})", code, data)
                return data
            except AMapError:
                raise
            except (requests.RequestException, ValueError) as e:
                last_error = e
                if attempt >= self.max_retries:
                    break
                time.sleep(min(2 ** attempt, 5))
        raise AMapError(f"{endpoint_name}: 网络请求失败：{last_error}")

    def test_key(self) -> tuple[bool, str]:
        data = self._get(
            "district",
            self.DISTRICT_URL,
            {"keywords": "中国", "subdistrict": 0, "extensions": "base"},
        )
        return True, data.get("info", "OK")

    def search_district(self, keyword: str, subdistrict: int = 1, extensions: str = "base") -> list[District]:
        data = self._get(
            "district",
            self.DISTRICT_URL,
            {
                "keywords": keyword,
                "subdistrict": subdistrict,
                "extensions": extensions,
                "offset": 20,
                "page": 1,
            },
        )
        return [self._district_from_dict(x) for x in ensure_list(data.get("districts"))]

    def get_district(self, adcode: str, subdistrict: int = 1, extensions: str = "all") -> District:
        items = self.search_district(adcode, subdistrict=subdistrict, extensions=extensions)
        if not items:
            raise AMapError(f"未找到行政区：{adcode}")
        exact = next((x for x in items if x.adcode == adcode), items[0])
        return exact

    @staticmethod
    def _district_from_dict(x: dict) -> District:
        return District(
            name=str(x.get("name", "")),
            adcode=str(x.get("adcode", "")),
            citycode=str(x.get("citycode", "") if not isinstance(x.get("citycode"), list) else ""),
            level=str(x.get("level", "")),
            center=str(x.get("center", "")),
            polyline=str(x.get("polyline", "")),
            districts=ensure_list(x.get("districts")),
        )

    def search_bus_stop_pois_rect(
        self,
        rect: tuple[float, float, float, float],
        page_num: int = 1,
        page_size: int = 25,
    ) -> dict:
        minx, miny, maxx, maxy = rect
        polygon = f"{minx:.6f},{maxy:.6f}|{maxx:.6f},{miny:.6f}"
        base_params = {
            "polygon": polygon,
            "page_size": page_size,
            "page_num": page_num,
        }
        # Prefer the documented Chinese category name, with a numeric fallback for compatibility.
        try:
            return self._get(
                "poi_polygon",
                self.POI_POLYGON_URL,
                {**base_params, "types": "公交车站"},
            )
        except AMapError as e:
            # Compatibility fallback for older category tables.
            if e.is_quota_error:
                raise
            return self._get(
                "poi_polygon",
                self.POI_POLYGON_URL,
                {**base_params, "types": "150700"},
            )

    def search_bus_stops(self, keyword: str, city: str, page: int = 1, offset: int = 100) -> list[dict]:
        data = self._get(
            "bus_stop_name",
            self.BUS_STOP_NAME_URL,
            {
                "keywords": keyword,
                "city": city,
                "page": page,
                "offset": min(offset, 100),
                "extensions": "base",
            },
        )
        return ensure_list(data.get("busstops"))

    def get_bus_line(self, line_id: str) -> list[dict]:
        data = self._get(
            "bus_line_id",
            self.BUS_LINE_ID_URL,
            {"id": line_id, "extensions": "all"},
        )
        return ensure_list(data.get("buslines"))

    def search_bus_lines(self, keyword: str, city: str, page: int = 1, offset: int = 100) -> list[dict]:
        data = self._get(
            "bus_line_name",
            self.BUS_LINE_NAME_URL,
            {
                "keywords": keyword,
                "city": city,
                "page": page,
                "offset": min(offset, 100),
                "extensions": "all",
            },
        )
        return ensure_list(data.get("buslines"))
