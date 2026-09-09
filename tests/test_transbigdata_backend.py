import tempfile
import unittest
from pathlib import Path

import geopandas as gpd
import pandas as pd
from shapely.geometry import LineString, Point

import transit_collector.transbigdata_adapter  # installs WGS84 -> GCJ-02 DB adapter
from transit_collector.db import TransitDB
from transit_collector.gis import GISProcessor
from transit_collector.transbigdata_backend import (
    DiscoveryConfig,
    ingest_transbigdata_frames,
    normalize_line_keyword,
)


class TransBigDataBackendTest(unittest.TestCase):
    def test_keyword_generation(self):
        full = DiscoveryConfig.full(["榆横城际公交"])
        keys = full.probe_keywords()
        self.assertIn("1路", keys)
        self.assertIn("999路", keys)
        self.assertIn("K199路", keys)
        self.assertIn("榆横城际公交", keys)

        quick = DiscoveryConfig.quick()
        qkeys = quick.probe_keywords()
        self.assertIn("300路", qkeys)
        self.assertNotIn("999路", qkeys)
        self.assertEqual(normalize_line_keyword("1路(汽车站-高铁站)"), "1路")

    def test_transbigdata_frames_share_existing_gis_pipeline(self):
        with tempfile.TemporaryDirectory() as td:
            db = TransitDB(Path(td) / "tbd.sqlite")
            try:
                line = gpd.GeoDataFrame(
                    {
                        "linename": ["测试1路(甲站-乙站)"],
                        "line": ["测试1路"],
                        "city": ["测试市"],
                    },
                    geometry=[LineString([(109.10, 38.20), (109.20, 38.30)])],
                    crs="EPSG:4326",
                )
                stop = gpd.GeoDataFrame(
                    {
                        "stationnames": ["甲站", "乙站"],
                        "linename": ["测试1路(甲站-乙站)", "测试1路(甲站-乙站)"],
                        "line": ["测试1路", "测试1路"],
                        "id": [1.0, 2.0],
                        "lon": [109.10, 109.20],
                        "lat": [38.20, 38.30],
                    },
                    geometry=[Point(109.10, 38.20), Point(109.20, 38.30)],
                    crs="EPSG:4326",
                )

                inserted = ingest_transbigdata_frames(db, "测试市", "999", line, stop)
                self.assertEqual(inserted, 1)
                db.rebuild_station_groups(120)
                counts = db.counts()
                self.assertEqual(counts["lines"], 1)
                self.assertEqual(counts["stations"], 2)
                self.assertEqual(db.get_meta("coordinate_system"), "GCJ-02")

                # GISProcessor should convert the normalized DB coordinates back
                # to approximately the original TransBigData WGS84 positions.
                stations = GISProcessor(db).stations_gdf().sort_values("name").reset_index(drop=True)
                self.assertEqual(len(stations), 2)
                coords = {(row["name"], round(row.geometry.x, 4), round(row.geometry.y, 4)) for _, row in stations.iterrows()}
                self.assertIn(("甲站", 109.1, 38.2), coords)
                self.assertIn(("乙站", 109.2, 38.3), coords)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
