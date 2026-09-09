from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from transit_collector.db import TransitDB


@dataclass(frozen=True)
class StationDependencyState:
    current: bool
    reason: str
    desired_radius_m: float
    stored_radius_m: float | None
    current_signature: str
    stored_signature: str
    station_count: int


def source_signature(db: TransitDB) -> str:
    """Stable lightweight signature for data that affects merged stations/GIS output."""
    payload = {}
    for table in ("bus_lines", "raw_stops", "line_stops", "raw_stop_lines"):
        row = db.conn.execute(
            f"SELECT COUNT(*) AS n, COALESCE(MAX(rowid),0) AS max_rowid FROM {table}"
        ).fetchone()
        payload[table] = [int(row["n"]), int(row["max_rowid"])]
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def is_partial_database(db: TransitDB) -> bool:
    return str(db.get_meta("completed", "0")) != "1"


def station_dependency_state(db: TransitDB, desired_radius_m: float) -> StationDependencyState:
    current_sig = source_signature(db)
    stored_sig = str(db.get_meta("station_source_signature", "") or "")
    stored_radius_raw = db.get_meta("station_merge_radius_m", None)
    try:
        stored_radius = float(stored_radius_raw) if stored_radius_raw not in (None, "") else None
    except (TypeError, ValueError):
        stored_radius = None
    station_count = int(db.conn.execute("SELECT COUNT(*) FROM station_groups").fetchone()[0])

    reasons = []
    if station_count <= 0:
        reasons.append("尚未生成合并站点")
    if stored_radius is None:
        reasons.append("数据库没有记录站点合并参数")
    elif abs(stored_radius - float(desired_radius_m)) > 1e-9:
        reasons.append(f"合并半径已由 {stored_radius:g}m 改为 {float(desired_radius_m):g}m")
    if not stored_sig:
        reasons.append("数据库没有记录站点来源版本")
    elif stored_sig != current_sig:
        reasons.append("原始线路/站点数据已发生变化")

    return StationDependencyState(
        current=not reasons,
        reason="；".join(reasons) if reasons else "最新",
        desired_radius_m=float(desired_radius_m),
        stored_radius_m=stored_radius,
        current_signature=current_sig,
        stored_signature=stored_sig,
        station_count=station_count,
    )


def rebuild_station_groups_tracked(db: TransitDB, radius_m: float) -> StationDependencyState:
    db.rebuild_station_groups(float(radius_m))
    sig = source_signature(db)
    db.set_meta("station_source_signature", sig)
    db.set_meta("station_merge_radius_m", str(float(radius_m)))
    db.set_meta("station_groups_dirty", "0")
    return station_dependency_state(db, float(radius_m))


def mark_station_groups_dirty(db: TransitDB, reason: str = "source_changed") -> None:
    db.set_meta("station_groups_dirty", "1")
    db.set_meta("station_groups_dirty_reason", reason)


def database_summary(db: TransitDB) -> dict:
    counts = db.counts()
    return {
        **counts,
        "completed": not is_partial_database(db),
        "data_source": db.get_meta("data_source", ""),
        "scope": db.get_meta("scope", ""),
        "coordinate_system": db.get_meta("coordinate_system", ""),
    }
