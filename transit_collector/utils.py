from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Sequence


def normalize_station_name(name: str) -> str:
    """Normalize station names for matching/merging while preserving the display name elsewhere."""
    name = (name or "").strip()
    # Common POI decorations
    name = re.sub(r"[（(]\s*公交站\s*[)）]$", "", name)
    name = re.sub(r"\s*公交站$", "", name)
    name = re.sub(r"\s+", "", name)
    name = name.replace("－", "-").replace("—", "-")
    return name


def parse_location(location: str | Sequence[float] | None) -> tuple[float | None, float | None]:
    if not location:
        return None, None
    if isinstance(location, (list, tuple)) and len(location) >= 2:
        try:
            return float(location[0]), float(location[1])
        except (TypeError, ValueError):
            return None, None
    try:
        lon, lat = str(location).split(",", 1)
        return float(lon), float(lat)
    except (ValueError, TypeError):
        return None, None


def haversine_m(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bbox_from_polyline(polyline: str) -> tuple[float, float, float, float]:
    xs, ys = [], []
    if not polyline:
        raise ValueError("行政区边界为空，无法建立采集网格")
    # AMap district polyline uses ';' between points and '|' between disjoint polygons.
    for part in polyline.split("|"):
        for token in part.split(";"):
            token = token.strip()
            if not token or "," not in token:
                continue
            try:
                x, y = token.split(",", 1)
                xs.append(float(x))
                ys.append(float(y))
            except ValueError:
                continue
    if not xs:
        raise ValueError("行政区边界解析失败")
    return min(xs), min(ys), max(xs), max(ys)


def rect_key(city_adcode: str, rect: tuple[float, float, float, float], depth: int) -> str:
    raw = f"{city_adcode}|{depth}|" + "|".join(f"{v:.6f}" for v in rect)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def stable_group_id(member_ids: Iterable[str]) -> str:
    raw = "|".join(sorted(str(x) for x in member_ids))
    return "STA_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def split_rect(rect: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
    minx, miny, maxx, maxy = rect
    mx, my = (minx + maxx) / 2, (miny + maxy) / 2
    return [
        (minx, miny, mx, my),
        (mx, miny, maxx, my),
        (minx, my, mx, maxy),
        (mx, my, maxx, maxy),
    ]


def rect_size_deg(rect: tuple[float, float, float, float]) -> float:
    minx, miny, maxx, maxy = rect
    return max(maxx - minx, maxy - miny)


def ensure_list(value):
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]
