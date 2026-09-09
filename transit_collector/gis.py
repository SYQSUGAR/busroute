from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import geopandas as gpd
from pyproj import CRS
from shapely.geometry import LineString, Point
from shapely.geometry.base import BaseGeometry

from transit_collector.db import TransitDB
from transit_collector.utils import parse_location


_A = 6378245.0
_EE = 0.00669342162296594323


def _out_of_china(lon: float, lat: float) -> bool:
    return not (72.004 <= lon <= 137.8347 and 0.8293 <= lat <= 55.8271)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lon: float, lat: float) -> tuple[float, float]:
    """Convert WGS84 to GCJ-02 for coordinates in mainland China."""
    if _out_of_china(lon, lat):
        return lon, lat
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lon + dlon, lat + dlat


def gcj02_to_wgs84(lon: float, lat: float, iterations: int = 3) -> tuple[float, float]:
    """Iteratively invert GCJ-02 to an approximate WGS84 coordinate."""
    if _out_of_china(lon, lat):
        return lon, lat
    wlon, wlat = lon, lat
    for _ in range(max(1, iterations)):
        glon, glat = wgs84_to_gcj02(wlon, wlat)
        wlon -= glon - lon
        wlat -= glat - lat
    return wlon, wlat


def _parse_polyline(polyline: str | None) -> list[tuple[float, float]]:
    if not polyline:
        return []
    text = str(polyline).strip()
    if text.startswith("["):
        try:
            value = json.loads(text)
            if isinstance(value, list):
                if value and isinstance(value[0], str):
                    text = ";".join(value)
                elif value and isinstance(value[0], (list, tuple)):
                    return [
                        gcj02_to_wgs84(float(p[0]), float(p[1]))
                        for p in value
                        if isinstance(p, (list, tuple)) and len(p) >= 2
                    ]
        except (ValueError, TypeError, json.JSONDecodeError):
            pass

    points: list[tuple[float, float]] = []
    for token in text.replace("|", ";").split(";"):
        lon, lat = parse_location(token)
        if lon is None or lat is None:
            continue
        points.append(gcj02_to_wgs84(lon, lat))
    return points


def _union_all(series: gpd.GeoSeries) -> BaseGeometry:
    if hasattr(series, "union_all"):
        return series.union_all()
    return series.unary_union


def _clean_for_shapefile(gdf: gpd.GeoDataFrame, field_map: dict[str, str]) -> gpd.GeoDataFrame:
    cols = [c for c in field_map if c in gdf.columns]
    out = gdf[cols + ["geometry"]].rename(columns={c: field_map[c] for c in cols}).copy()
    for col in out.columns:
        if col == "geometry":
            continue
        if out[col].dtype == "object":
            out[col] = out[col].fillna("").astype(str).str.slice(0, 250)
    return out


class GISProcessor:
    """GeoPandas/Shapely vector layer builder and coverage analyzer.

    AMap data is GCJ-02. Standards-compliant GIS layers are written after an
    approximate inverse conversion to WGS84 (EPSG:4326). Metric analysis is
    performed in an automatically selected local UTM CRS, or in a user-supplied
    projected CRS whose linear unit is metre.
    """

    def __init__(self, db: TransitDB):
        self.db = db

    def stations_gdf(self) -> gpd.GeoDataFrame:
        rows = self.db.conn.execute(
            """
            SELECT sg.station_id,sg.name,sg.longitude,sg.latitude,sg.member_count,sg.line_count,
                   GROUP_CONCAT(DISTINCT sl.line_id) AS line_ids,
                   GROUP_CONCAT(DISTINCT bl.name) AS line_names
            FROM station_groups sg
            LEFT JOIN station_lines sl ON sl.station_id=sg.station_id
            LEFT JOIN bus_lines bl ON bl.line_id=sl.line_id
            GROUP BY sg.station_id
            ORDER BY sg.name,sg.station_id
            """
        ).fetchall()
        records, geoms = [], []
        for row in rows:
            if row["longitude"] is None or row["latitude"] is None:
                continue
            lon, lat = gcj02_to_wgs84(float(row["longitude"]), float(row["latitude"]))
            records.append(
                {
                    "station_id": row["station_id"],
                    "name": row["name"],
                    "member_count": row["member_count"],
                    "line_count": row["line_count"],
                    "line_ids": row["line_ids"] or "",
                    "line_names": row["line_names"] or "",
                    "source_crs": "GCJ-02",
                }
            )
            geoms.append(Point(lon, lat))
        return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")

    def routes_gdf(self) -> gpd.GeoDataFrame:
        rows = self.db.conn.execute(
            """
            SELECT line_id,name,type,start_stop,end_stop,start_time,end_time,distance,loop,status,
                   direc,company,basic_price,total_price,crawl_city_adcode,polyline
            FROM bus_lines ORDER BY crawl_city_adcode,name,line_id
            """
        ).fetchall()
        records, geoms = [], []
        for row in rows:
            coords = _parse_polyline(row["polyline"])
            if len(coords) < 2:
                continue
            records.append(
                {
                    "line_id": row["line_id"],
                    "name": row["name"],
                    "type": row["type"],
                    "start_stop": row["start_stop"],
                    "end_stop": row["end_stop"],
                    "start_time": row["start_time"],
                    "end_time": row["end_time"],
                    "distance": row["distance"],
                    "loop": row["loop"],
                    "status": row["status"],
                    "direction": row["direc"],
                    "company": row["company"],
                    "basic_price": row["basic_price"],
                    "total_price": row["total_price"],
                    "city_adcode": row["crawl_city_adcode"],
                    "source_crs": "GCJ-02",
                }
            )
            geoms.append(LineString(coords))
        return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")

    def route_stops_gdf(self) -> gpd.GeoDataFrame:
        rows = self.db.conn.execute(
            """
            SELECT ls.line_id,bl.name AS line_name,ls.sequence,
                   COALESCE(sm.station_id,'') AS station_id,
                   ls.stop_id AS raw_stop_id,ls.stop_name,ls.longitude,ls.latitude
            FROM line_stops ls
            JOIN bus_lines bl ON bl.line_id=ls.line_id
            LEFT JOIN station_members sm ON sm.stop_id=ls.stop_id
            ORDER BY bl.name,ls.line_id,ls.sequence
            """
        ).fetchall()
        records, geoms = [], []
        for row in rows:
            if row["longitude"] is None or row["latitude"] is None:
                continue
            lon, lat = gcj02_to_wgs84(float(row["longitude"]), float(row["latitude"]))
            records.append(
                {
                    "line_id": row["line_id"],
                    "line_name": row["line_name"],
                    "sequence": row["sequence"],
                    "station_id": row["station_id"],
                    "raw_stop_id": row["raw_stop_id"],
                    "stop_name": row["stop_name"],
                    "source_crs": "GCJ-02",
                }
            )
            geoms.append(Point(lon, lat))
        return gpd.GeoDataFrame(records, geometry=geoms, crs="EPSG:4326")

    def export_base_layers(self, out_dir: str | Path, write_shp: bool = True) -> dict[str, str]:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        gpkg = out / "transit_gis.gpkg"
        if gpkg.exists():
            gpkg.unlink()

        stations = self.stations_gdf()
        routes = self.routes_gdf()
        route_stops = self.route_stops_gdf()
        result: dict[str, str] = {}

        for layer_name, gdf in (
            ("bus_stations", stations),
            ("bus_routes", routes),
            ("route_stops", route_stops),
        ):
            if not gdf.empty:
                gdf.to_file(gpkg, layer=layer_name, driver="GPKG", engine="pyogrio")

        if gpkg.exists():
            result["gpkg"] = str(gpkg)

        station_geojson = out / "stations_wgs84.geojson"
        route_geojson = out / "bus_routes_wgs84.geojson"
        if not stations.empty:
            stations.to_file(station_geojson, driver="GeoJSON", engine="pyogrio")
            result["stations_geojson"] = str(station_geojson)
        if not routes.empty:
            routes.to_file(route_geojson, driver="GeoJSON", engine="pyogrio")
            result["lines_geojson"] = str(route_geojson)

        if write_shp:
            shp_dir = out / "shp"
            shp_dir.mkdir(parents=True, exist_ok=True)

            if not stations.empty:
                shp_stations = _clean_for_shapefile(
                    stations,
                    {
                        "station_id": "sta_id",
                        "name": "name",
                        "member_count": "member_cnt",
                        "line_count": "line_cnt",
                        "line_ids": "line_ids",
                        "line_names": "lines",
                        "source_crs": "src_crs",
                    },
                )
                p = shp_dir / "bus_stations.shp"
                shp_stations.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                result["stations_shp"] = str(p)

            if not routes.empty:
                shp_routes = _clean_for_shapefile(
                    routes,
                    {
                        "line_id": "line_id",
                        "name": "name",
                        "type": "type",
                        "start_stop": "start",
                        "end_stop": "end",
                        "start_time": "start_tm",
                        "end_time": "end_tm",
                        "distance": "distance",
                        "status": "status",
                        "direction": "direc",
                        "company": "company",
                        "city_adcode": "adcode",
                        "source_crs": "src_crs",
                    },
                )
                p = shp_dir / "bus_routes.shp"
                shp_routes.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                result["routes_shp"] = str(p)

            if not route_stops.empty:
                shp_route_stops = _clean_for_shapefile(
                    route_stops,
                    {
                        "line_id": "line_id",
                        "line_name": "line_name",
                        "sequence": "seq",
                        "station_id": "sta_id",
                        "raw_stop_id": "raw_id",
                        "stop_name": "stop_name",
                        "source_crs": "src_crs",
                    },
                )
                p = shp_dir / "route_stops.shp"
                shp_route_stops.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                result["route_stops_shp"] = str(p)

        meta_path = out / "gis_metadata.json"
        meta_path.write_text(
            json.dumps(
                {
                    "source_coordinate_system": "AMap GCJ-02",
                    "export_coordinate_reference_system": (
                        "EPSG:4326, using an approximate iterative GCJ-02 -> WGS84 inverse conversion"
                    ),
                    "primary_spatial_format": "GeoPackage",
                    "layers": ["bus_stations", "bus_routes", "route_stops"],
                    "shapefile_note": "Shapefile is compatibility output; field names/text lengths are constrained.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            "utf-8",
        )
        result["gis_metadata"] = str(meta_path)
        return result

    @staticmethod
    def read_boundary(path: str | Path, layer: str | None = None) -> gpd.GeoDataFrame:
        kwargs: dict[str, Any] = {"engine": "pyogrio"}
        if layer:
            kwargs["layer"] = layer
        gdf = gpd.read_file(path, **kwargs)
        if gdf.empty:
            raise ValueError("分析范围图层为空。")
        if gdf.crs is None:
            raise ValueError("分析范围图层缺少 CRS，无法进行可靠的距离/面积计算。请先定义正确坐标系。")
        gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].copy()
        if gdf.empty:
            raise ValueError("分析范围中没有有效几何。")
        if hasattr(gdf.geometry, "make_valid"):
            gdf.geometry = gdf.geometry.make_valid()
        gdf = gdf[gdf.geometry.geom_type.isin(["Polygon", "MultiPolygon"])].copy()
        if gdf.empty:
            raise ValueError("覆盖率分析要求输入 Polygon / MultiPolygon 范围图层。")
        return gdf

    @staticmethod
    def choose_metric_crs(boundary: gpd.GeoDataFrame, explicit_crs: str | None = None) -> CRS:
        if explicit_crs:
            crs = CRS.from_user_input(explicit_crs)
            if not crs.is_projected:
                raise ValueError("手动指定的分析 CRS 必须是投影坐标系。")
            axis = crs.axis_info[0] if crs.axis_info else None
            factor = getattr(axis, "unit_conversion_factor", None)
            if factor is not None and abs(float(factor) - 1.0) > 1e-9:
                raise ValueError("手动指定的分析 CRS 线性单位必须为米。")
            return crs
        wgs = boundary.to_crs("EPSG:4326")
        estimated = wgs.estimate_utm_crs()
        if estimated is None:
            raise ValueError("无法自动估算米制投影坐标系，请手动指定 EPSG/CRS。")
        return CRS.from_user_input(estimated)

    def analyze_station_coverage(
        self,
        boundary_path: str | Path,
        out_dir: str | Path,
        buffer_m: float = 500.0,
        boundary_layer: str | None = None,
        metric_crs: str | None = None,
        write_shp: bool = True,
    ) -> dict[str, Any]:
        """Calculate station-buffer area coverage for a polygon layer.

        The input polygon layer is the denominator. It can therefore represent
        built-up area, residential land, a street/community layer, or any other
        user-defined analysis scope. If it contains multiple polygons, coverage
        statistics are also calculated for each feature.
        """
        if buffer_m <= 0:
            raise ValueError("服务半径必须大于 0 米。")

        stations = self.stations_gdf()
        if stations.empty:
            raise ValueError("数据库中没有可用于分析的合并公交站点。")

        boundary = self.read_boundary(boundary_path, boundary_layer)
        metric = self.choose_metric_crs(boundary, metric_crs)
        boundary_metric = boundary.to_crs(metric)
        stations_metric = stations.to_crs(metric)

        boundary_union = _union_all(boundary_metric.geometry)
        influence = boundary_union.buffer(float(buffer_m))
        stations_metric = stations_metric[stations_metric.geometry.intersects(influence)].copy()

        if stations_metric.empty:
            service_union = boundary_union.difference(boundary_union)
        else:
            service_union = _union_all(stations_metric.geometry.buffer(float(buffer_m)))

        covered_geom = boundary_union.intersection(service_union)
        uncovered_geom = boundary_union.difference(service_union)
        total_area = float(boundary_union.area)
        covered_area = float(covered_geom.area)
        coverage_pct = covered_area / total_area * 100.0 if total_area > 0 else 0.0

        zones_metric = boundary_metric.copy()
        zones_metric["area_m2"] = zones_metric.geometry.area
        zones_metric["covered_m2"] = zones_metric.geometry.intersection(service_union).area
        zones_metric["coverage_pct"] = (
            zones_metric["covered_m2"] / zones_metric["area_m2"].where(zones_metric["area_m2"] > 0)
        ).fillna(0.0) * 100.0
        zones_out = zones_metric.to_crs(boundary.crs)

        original_fields = [c for c in boundary.columns if c != "geometry"]
        preferred_label_fields = [
            "name", "NAME", "Name", "名称", "街道", "社区", "小区", "村", "乡镇",
            "district", "community", "id", "ID", "OBJECTID", "FID",
        ]
        label_field = next((c for c in preferred_label_fields if c in original_fields), None)
        if label_field is None and original_fields:
            label_field = original_fields[0]

        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        gpkg = out / "coverage_analysis.gpkg"
        if gpkg.exists():
            gpkg.unlink()

        zones_out.to_file(gpkg, layer="analysis_zones", driver="GPKG", engine="pyogrio")

        service_gdf = gpd.GeoDataFrame(
            [{"buffer_m": float(buffer_m), "station_count": int(len(stations_metric))}],
            geometry=[service_union],
            crs=metric,
        ).to_crs(boundary.crs)
        covered_gdf = gpd.GeoDataFrame(
            [{"buffer_m": float(buffer_m), "coverage_pct": coverage_pct, "area_m2": covered_area}],
            geometry=[covered_geom],
            crs=metric,
        ).to_crs(boundary.crs)
        uncovered_gdf = gpd.GeoDataFrame(
            [{"buffer_m": float(buffer_m), "area_m2": max(total_area - covered_area, 0.0)}],
            geometry=[uncovered_geom],
            crs=metric,
        ).to_crs(boundary.crs)

        if not service_gdf.geometry.iloc[0].is_empty:
            service_gdf.to_file(gpkg, layer="service_area", driver="GPKG", engine="pyogrio")
        if not covered_gdf.geometry.iloc[0].is_empty:
            covered_gdf.to_file(gpkg, layer="covered_area", driver="GPKG", engine="pyogrio")
        if not uncovered_gdf.geometry.iloc[0].is_empty:
            uncovered_gdf.to_file(gpkg, layer="uncovered_area", driver="GPKG", engine="pyogrio")

        result: dict[str, Any] = {
            "gpkg": str(gpkg),
            "buffer_m": float(buffer_m),
            "metric_crs": metric.to_string(),
            "boundary_crs": str(boundary.crs),
            "station_count": int(len(stations_metric)),
            "total_area_m2": total_area,
            "covered_area_m2": covered_area,
            "coverage_pct": coverage_pct,
            "label_field": label_field or "",
        }

        zone_stats = []
        for idx, row in zones_metric.iterrows():
            label = str(row[label_field]) if label_field and row.get(label_field) is not None else str(idx)
            zone_stats.append(
                {
                    "row_id": str(idx),
                    "label": label,
                    "area_m2": float(row["area_m2"]),
                    "covered_m2": float(row["covered_m2"]),
                    "coverage_pct": float(row["coverage_pct"]),
                }
            )
        result["zone_stats"] = zone_stats

        if write_shp:
            shp_dir = out / "shp"
            shp_dir.mkdir(parents=True, exist_ok=True)

            zone_shp = gpd.GeoDataFrame(
                {
                    "zone_id": [str(i) for i in zones_out.index],
                    "label": [
                        str(v)[:250] if label_field else str(i)
                        for i, v in zip(zones_out.index, zones_out[label_field] if label_field else zones_out.index)
                    ],
                    "area_m2": zones_out["area_m2"].astype(float),
                    "cover_m2": zones_out["covered_m2"].astype(float),
                    "cover_pct": zones_out["coverage_pct"].astype(float),
                },
                geometry=zones_out.geometry,
                crs=zones_out.crs,
            )
            p = shp_dir / "coverage_zones.shp"
            zone_shp.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
            result["zones_shp"] = str(p)

            service_shp = _clean_for_shapefile(
                service_gdf, {"buffer_m": "buffer_m", "station_count": "sta_cnt"}
            )
            covered_shp = _clean_for_shapefile(
                covered_gdf, {"buffer_m": "buffer_m", "coverage_pct": "cover_pct", "area_m2": "area_m2"}
            )
            uncovered_shp = _clean_for_shapefile(
                uncovered_gdf, {"buffer_m": "buffer_m", "area_m2": "area_m2"}
            )
            for name, gdf in (
                ("service_area", service_shp),
                ("covered_area", covered_shp),
                ("uncovered_area", uncovered_shp),
            ):
                if gdf.geometry.iloc[0].is_empty:
                    continue
                p = shp_dir / f"{name}.shp"
                gdf.to_file(p, driver="ESRI Shapefile", engine="pyogrio", encoding="UTF-8")
                result[f"{name}_shp"] = str(p)

        summary_path = out / "coverage_summary.json"
        summary_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), "utf-8")
        result["summary_json"] = str(summary_path)
        return result
