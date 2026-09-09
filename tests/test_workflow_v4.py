import json
import tempfile
import unittest
from pathlib import Path

from transit_collector.db import TransitDB
from transit_collector.project import ProjectManager, TaskState
from transit_collector.transbigdata_backend import DiscoveryConfig
import transit_collector.transbigdata_v4 as tbd_v4
from transit_collector.workflow import rebuild_station_groups_tracked, station_dependency_state


class WorkflowV4Test(unittest.TestCase):
    def _line(self, line_id, name, suffix=""):
        return {
            "id": line_id,
            "name": name,
            "start_stop": "甲站",
            "end_stop": "乙站",
            "polyline": "109.100000,38.200000;109.200000,38.300000",
            "busstops": [
                {"id": f"{line_id}_a{suffix}", "name": "甲站", "location": "109.100000,38.200000", "sequence": 1},
                {"id": f"{line_id}_b{suffix}", "name": "乙站", "location": "109.200000,38.300000", "sequence": 2},
            ],
        }

    def test_project_manifest_and_station_dependency(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "transit.sqlite"
            db = TransitDB(db_path)
            try:
                db.upsert_line(self._line("L1", "1路"), "610800")
                db.set_meta("completed", "1")
                state = rebuild_station_groups_tracked(db, 120)
                self.assertTrue(state.current)
                self.assertEqual(state.station_count, 2)

                changed_radius = station_dependency_state(db, 80)
                self.assertFalse(changed_radius.current)
                self.assertIn("120", changed_radius.reason)

                db.upsert_line(self._line("L2", "2路", "x"), "610800")
                changed_source = station_dependency_state(db, 120)
                self.assertFalse(changed_source.current)
                self.assertIn("原始线路/站点数据已发生变化", changed_source.reason)
            finally:
                db.close()

            project = ProjectManager.for_database(
                db_path, name="榆林市公交数据", city="榆林市", source="TransBigData/Baidu"
            )
            project.mark_state(TaskState.PAUSED, data_status="partial")
            run_id, log = project.start_run("collect", {"city": "榆林市"})
            project.append_log("hello")
            project.finish_run("paused", "test")
            project.register_artifact("test", db_path)

            self.assertTrue(project.manifest_path.exists())
            self.assertTrue(log.exists())
            data = json.loads(project.manifest_path.read_text("utf-8"))
            self.assertEqual(data["state"], TaskState.PAUSED)
            self.assertEqual(data["last_run"]["run_id"], run_id)
            self.assertEqual(data["artifacts"][-1]["kind"], "test")

    def test_transbigdata_v4_retry_schema(self):
        with tempfile.TemporaryDirectory() as td:
            db = TransitDB(Path(td) / "retry.sqlite")
            try:
                discoverer = tbd_v4.ResumableBaiduLineDiscoverer(
                    city="榆林市",
                    db=db,
                    config=DiscoveryConfig.quick(),
                )
                self.assertIsNotNone(discoverer)
                probe_cols = {r[1] for r in db.conn.execute("PRAGMA table_info(tbd_probe)").fetchall()}
                self.assertIn("retry_count", probe_cols)
                broad = db.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='tbd_broad_probe'"
                ).fetchone()
                self.assertIsNotNone(broad)
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
