import tempfile
import unittest
from pathlib import Path

from transit_collector.db import TransitDB
from transit_collector.exporter import Exporter


class CoreDataTest(unittest.TestCase):
    def test_station_merge_and_export(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td)
            db = TransitDB(base / "test.sqlite")
            try:
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
                counts = db.counts()
                self.assertEqual(counts["lines"], 1)
                self.assertEqual(counts["stations"], 2)

                out = base / "export"
                files = Exporter(db).export_all(out)
                self.assertTrue(Path(files["xlsx"]).exists())
                self.assertTrue(Path(files["stations_geojson"]).exists())
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
