import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pyogrio
from shapely.geometry import box

from transit_collector.db import TransitDB
from transit_collector.gis import GISProcessor, gcj02_to_wgs84, wgs84_to_gcj02


class GISProcessingTest(unittest.TestCase):
    def _make_db(self, path: Path) -> TransitDB:
        db = TransitDB(path)
        db.upsert_raw_stop(
            {"id": "S1", "name": "测试站", "location": "109.100000,38.200000", "adcode": "610802"},
            "test",
        )
        db.upsert_raw_stop(
            {"id": "S2", "name": "测试站", "location": "109.100300,38.200200", "adcode": "610802"},
            "test",
        )
        db.upsert_line(
            {
                "id": "L1",
                "name": "1路",
                "type": "公交",
                "start_stop": "测试站",
                "end_stop": "终点站",
                "polyline": "109.1,38.2;109.2,38.3",
                "busstops": [
                    {"id": "S1", "name": "测试站", "location": "109.100000,38.200000", "sequence": "1"},
                    {"id": "S3", "name": "终点站", "location": "109.200000,38.300000", "sequence": "2"},
                ],
            },
            "610800",
        )
        db.rebuild_station_groups(120)
        return db

    def test_gcj_inverse_round_trip(self):
        lon, lat = 109.1, 38.2
        gcj = wgs84_to_gcj02(lon, lat)
        restored = gcj02_to_wgs84(*gcj)
        self.assertAlmostEqual(restored[0], lon, places=6)
        self.assertAlmostEqual(restored[1], lat, places=6)

    def test_vector_export_and_coverage(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = self._make_db(base / "test.sqlite")
            try:
                proc = GISProcessor(db)
                stations = proc.stations_gdf()
                routes = proc.routes_gdf()
                route_stops = proc.route_stops_gdf()
                self.assertEqual(stations.crs.to_epsg(), 4326)
                self.assertEqual(len(routes), 1)
                self.assertEqual(len(route_stops), 2)

                out = base / "gis"
                files = proc.export_base_layers(out, write_shp=True)
                self.assertTrue(Path(files["gpkg"]).exists())
                self.assertTrue(Path(files["stations_shp"]).exists())
                layers = {row[0] for row in pyogrio.list_layers(files["gpkg"])}
                self.assertEqual(layers, {"bus_stations", "bus_routes", "route_stops"})

                minx, miny, maxx, maxy = stations.total_bounds
                boundary = gpd.GeoDataFrame(
                    {"name": ["测试范围"]},
                    geometry=[box(minx - 0.01, miny - 0.01, maxx + 0.01, maxy + 0.01)],
                    crs="EPSG:4326",
                )
                boundary_path = base / "boundary.gpkg"
                boundary.to_file(boundary_path, layer="boundary", driver="GPKG", engine="pyogrio")

                result = proc.analyze_station_coverage(
                    boundary_path,
                    base / "coverage",
                    buffer_m=500,
                    boundary_layer="boundary",
                    write_shp=True,
                )
                self.assertGreater(result["coverage_pct"], 0)
                self.assertLessEqual(result["coverage_pct"], 100)
                self.assertEqual(result["station_count"], 2)
                self.assertTrue(Path(result["gpkg"]).exists())
                self.assertTrue(Path(result["zones_shp"]).exists())
                self.assertEqual(result["zone_stats"][0]["label"], "测试范围")
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
