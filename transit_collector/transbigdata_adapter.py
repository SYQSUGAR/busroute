from __future__ import annotations

"""Compatibility adapter between TransBigData and the existing collector database.

TransBigData.getbusdata returns WGS84 geometry, while the original AMap collector
stores raw coordinates as GCJ-02 and GISProcessor converts the database back to
WGS84 on export. To keep one database/GIS pipeline for both sources, this module
normalizes TransBigData frames to GCJ-02 immediately before ingestion.
"""

from shapely.geometry import LineString, MultiLineString, Point

from transit_collector import transbigdata_backend as _backend
from transit_collector.gis import wgs84_to_gcj02


_ORIGINAL_INGEST = _backend.ingest_transbigdata_frames
_INSTALLED = False


def _geom_to_gcj02(geom):
    if geom is None or getattr(geom, "is_empty", True):
        return geom
    if isinstance(geom, Point):
        x, y = wgs84_to_gcj02(float(geom.x), float(geom.y))
        return Point(x, y)
    if isinstance(geom, LineString):
        return LineString([wgs84_to_gcj02(float(x), float(y)) for x, y in geom.coords])
    if isinstance(geom, MultiLineString):
        return MultiLineString(
            [LineString([wgs84_to_gcj02(float(x), float(y)) for x, y in part.coords]) for part in geom.geoms]
        )
    return geom


def _normalized_ingest(db, city, city_code, line_gdf, stop_gdf):
    lines = line_gdf.copy() if line_gdf is not None else line_gdf
    stops = stop_gdf.copy() if stop_gdf is not None else stop_gdf

    if lines is not None and len(lines) and "geometry" in lines.columns:
        lines["geometry"] = lines.geometry.apply(_geom_to_gcj02)

    if stops is not None and len(stops):
        if "lon" in stops.columns and "lat" in stops.columns:
            converted = []
            for lon, lat in zip(stops["lon"], stops["lat"]):
                try:
                    converted.append(wgs84_to_gcj02(float(lon), float(lat)))
                except (TypeError, ValueError):
                    converted.append((None, None))
            stops["lon"] = [p[0] for p in converted]
            stops["lat"] = [p[1] for p in converted]
        if "geometry" in stops.columns:
            stops["geometry"] = stops.geometry.apply(_geom_to_gcj02)

    inserted = _ORIGINAL_INGEST(db, city, city_code, lines, stops)
    db.set_meta("coordinate_system", "GCJ-02")
    db.set_meta("source_coordinate_system", "TransBigData/Baidu WGS84 -> normalized GCJ-02")
    return inserted


def install_transbigdata_adapter():
    global _INSTALLED
    if not _INSTALLED:
        _backend.ingest_transbigdata_frames = _normalized_ingest
        _INSTALLED = True


# Importing this module from app.py installs the adapter once.
install_transbigdata_adapter()

DiscoveryConfig = _backend.DiscoveryConfig
TransBigDataCollector = _backend.TransBigDataCollector
BaiduLineDiscoverer = _backend.BaiduLineDiscoverer
