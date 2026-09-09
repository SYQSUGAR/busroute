from __future__ import annotations

"""v0.4 reliability layer for the TransBigData/Baidu collector.

It upgrades the existing v0.3 implementation without duplicating its public API:
- broad-search seed/page progress is persisted individually;
- failed keyword probes are retried up to a bounded limit;
- failed TransBigData batches are recursively split to isolate bad lines;
- collection is marked partial when final failures remain.

Import this module before importing the desktop window. It patches the classes in
``transit_collector.transbigdata_backend`` so existing UI code remains compatible.
"""

import contextlib
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

from transit_collector import transbigdata_backend as _backend
from transit_collector.workflow import rebuild_station_groups_tracked


_BASE_DISCOVERER = _backend.BaiduLineDiscoverer
_BASE_COLLECTOR = _backend.TransBigDataCollector
MAX_RETRIES = 3


def _ensure_column(conn, table: str, column: str, ddl: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        conn.commit()


class ResumableBaiduLineDiscoverer(_BASE_DISCOVERER):
    def _ensure_schema(self):
        super()._ensure_schema()
        _ensure_column(self.db.conn, "tbd_probe", "retry_count", "INTEGER NOT NULL DEFAULT 0")
        self.db.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tbd_broad_probe (
                seed TEXT NOT NULL,
                page INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                route_names TEXT,
                last_error TEXT,
                PRIMARY KEY(seed,page)
            );
            """
        )
        self.db.conn.commit()

    def _broad_discovery(self):
        tasks = [
            (seed, page)
            for seed in self.config.broad_seeds
            for page in range(self.config.broad_pages)
        ]
        self.db.conn.executemany(
            "INSERT OR IGNORE INTO tbd_broad_probe(seed,page,status) VALUES(?,?,'pending')",
            tasks,
        )
        self.db.conn.commit()
        rows = self.db.conn.execute(
            """
            SELECT seed,page,retry_count
            FROM tbd_broad_probe
            WHERE status IN ('pending','error') AND retry_count < ?
            ORDER BY seed,page
            """,
            (MAX_RETRIES,),
        ).fetchall()
        if not rows:
            return

        self.log(f"阶段 1/3：恢复/执行 {len(rows)} 个命名线路分页搜索任务。")
        for index, row in enumerate(rows, start=1):
            self._check_cancel()
            seed, page, retries = row[0], int(row[1]), int(row[2])
            self.progress(f"{self.city} · 命名线路发现", index, max(len(rows), 1))
            try:
                data = self._search(seed, page)
                items = self._route_items(data, exact=False)
                names = {
                    _backend.normalize_line_keyword(x.get("name", ""))
                    for x in items
                    if isinstance(x, dict)
                }
                names.discard("")
                self._add_discovered(names, "baidu-broad")
                self.db.conn.execute(
                    """
                    UPDATE tbd_broad_probe
                    SET status='done',route_names=?,last_error=''
                    WHERE seed=? AND page=?
                    """,
                    (json.dumps(sorted(names), ensure_ascii=False), seed, page),
                )
            except Exception as exc:
                next_retry = retries + 1
                status = "failed_final" if next_retry >= MAX_RETRIES else "error"
                self.db.conn.execute(
                    """
                    UPDATE tbd_broad_probe
                    SET status=?,retry_count=?,last_error=?
                    WHERE seed=? AND page=?
                    """,
                    (status, next_retry, str(exc), seed, page),
                )
                self.log(
                    f"命名线路搜索失败：{seed} 第{page + 1}页 "
                    f"({next_retry}/{MAX_RETRIES}) -> {exc}"
                )
            self.db.conn.commit()

        count = self.db.conn.execute("SELECT COUNT(*) FROM tbd_line_keywords").fetchone()[0]
        self.log(f"命名线路阶段结束，当前累计发现 {count} 个线路关键词。")

    def _exact_probe(self):
        candidates = self.config.probe_keywords()
        self.db.conn.executemany(
            "INSERT OR IGNORE INTO tbd_probe(keyword,status,retry_count) VALUES(?,'pending',0)",
            [(x,) for x in candidates],
        )
        self.db.conn.commit()
        rows = self.db.conn.execute(
            """
            SELECT keyword,retry_count
            FROM tbd_probe
            WHERE status IN ('pending','error') AND COALESCE(retry_count,0) < ?
            ORDER BY keyword
            """,
            (MAX_RETRIES,),
        ).fetchall()
        pending = [(r[0], int(r[1] or 0)) for r in rows]
        if not pending:
            return

        self.log(
            f"阶段 2/3：线路名扫描 {len(pending)} 个待探测/重试关键词；"
            f"模式={self.config.mode}，并发={self.config.workers}。"
        )
        done = 0
        retry_map = {keyword: retries for keyword, retries in pending}
        with ThreadPoolExecutor(max_workers=max(1, self.config.workers)) as pool:
            future_map = {pool.submit(self._probe_one, keyword): keyword for keyword, _ in pending}
            for future in as_completed(future_map):
                self._check_cancel()
                keyword, names, error = future.result()
                done += 1
                if error == "cancelled":
                    continue
                if error:
                    next_retry = retry_map.get(keyword, 0) + 1
                    status = "failed_final" if next_retry >= MAX_RETRIES else "error"
                    self.db.conn.execute(
                        """
                        UPDATE tbd_probe
                        SET status=?,retry_count=?,route_names='[]',last_error=?
                        WHERE keyword=?
                        """,
                        (status, next_retry, error, keyword),
                    )
                else:
                    self.db.conn.execute(
                        """
                        UPDATE tbd_probe
                        SET status='done',route_names=?,last_error=''
                        WHERE keyword=?
                        """,
                        (json.dumps(names, ensure_ascii=False), keyword),
                    )
                    if names:
                        self._add_discovered(names, "baidu-exact-scan")

                if done % 25 == 0 or done == len(pending):
                    self.db.conn.commit()
                    count = self.db.conn.execute("SELECT COUNT(*) FROM tbd_line_keywords").fetchone()[0]
                    self.progress(f"{self.city} · 扫描线路名（已发现{count}）", done, len(pending))
        self.db.conn.commit()


class ReliableTransBigDataCollector(_BASE_COLLECTOR):
    def _ensure_schema(self):
        super()._ensure_schema()
        _ensure_column(self.db.conn, "tbd_fetch_state", "retry_count", "INTEGER NOT NULL DEFAULT 0")

    def _mark_single_failure(self, line_name: str, error: str) -> None:
        row = self.db.conn.execute(
            "SELECT COALESCE(retry_count,0) FROM tbd_fetch_state WHERE line_name=?",
            (line_name,),
        ).fetchone()
        retries = int(row[0] if row else 0) + 1
        status = "failed_final" if retries >= MAX_RETRIES else "error"
        self.db.conn.execute(
            """
            UPDATE tbd_fetch_state
            SET status=?,retry_count=?,last_error=?
            WHERE line_name=?
            """,
            (status, retries, error, line_name),
        )
        self.db.conn.commit()

    def _fetch_batch_recursive(self, tbd, batch: list[str], city_code: str) -> None:
        self._check_cancel()
        if not batch:
            return
        writer = _backend._LogWriter(self.log)
        try:
            with contextlib.redirect_stdout(writer):
                line_gdf, stop_gdf = tbd.getbusdata(
                    city=self.city,
                    keywords=batch,
                    accurate=True,
                    timeout=self.config.timeout,
                )
            writer.flush()
            _backend.ingest_transbigdata_frames(self.db, self.city, city_code, line_gdf, stop_gdf)
            self.db.conn.executemany(
                """
                UPDATE tbd_fetch_state
                SET status='done',last_error=''
                WHERE line_name=?
                """,
                [(x,) for x in batch],
            )
            self.db.conn.commit()
            return
        except InterruptedError:
            writer.flush()
            raise
        except Exception as exc:
            writer.flush()
            if len(batch) > 1:
                mid = max(1, len(batch) // 2)
                left, right = batch[:mid], batch[mid:]
                self.log(
                    f"TransBigData 批次失败，自动拆分以隔离异常线路："
                    f"{len(batch)} -> {len(left)} + {len(right)}；{exc}"
                )
                self._fetch_batch_recursive(tbd, left, city_code)
                self._fetch_batch_recursive(tbd, right, city_code)
                return
            self._mark_single_failure(batch[0], str(exc))
            self.log(f"线路 {batch[0]} 获取失败，已记录重试状态：{exc}")

    def run(self):
        self.db.set_meta("scope", {"name": self.city, "level": "city", "source": "TransBigData/Baidu"})
        self.db.set_meta("data_source", "TransBigData/Baidu")
        self.db.set_meta("coordinate_system", "WGS84")
        self.db.set_meta("completed", "0")
        self.db.set_meta("task_state", "DISCOVERING")

        discoverer = _backend.BaiduLineDiscoverer(
            city=self.city,
            db=self.db,
            config=self.config,
            cancel_event=self.cancel_event,
            log=self.log,
            progress=self.progress,
        )
        city_code, line_names = discoverer.discover()
        self.discovery_requests = discoverer.request_count
        self._emit_metrics()
        self.log(f"共发现 {len(line_names)} 个公交线路关键词，开始交给 TransBigData 获取线路和站点。")

        self.db.conn.executemany(
            "INSERT OR IGNORE INTO tbd_fetch_state(line_name,status,retry_count) VALUES(?,'pending',0)",
            [(x,) for x in line_names],
        )
        self.db.conn.commit()

        try:
            import transbigdata as tbd
        except ImportError as exc:
            raise RuntimeError("缺少 transbigdata，请重新运行 requirements.txt 安装依赖。") from exc

        pending_rows = self.db.conn.execute(
            """
            SELECT line_name
            FROM tbd_fetch_state
            WHERE status IN ('pending','error') AND COALESCE(retry_count,0) < ?
            ORDER BY line_name
            """,
            (MAX_RETRIES,),
        ).fetchall()
        pending = [r[0] for r in pending_rows]
        self.db.set_meta("task_state", "CRAWLING")
        self.log(f"阶段 3/3：TransBigData 待抓取/重试 {len(pending)} 个线路关键词。")

        batch_size = max(1, int(self.config.batch_size))
        total = len(pending)
        for start in range(0, total, batch_size):
            self._check_cancel()
            batch = pending[start:start + batch_size]
            self.progress(
                f"{self.city} · TransBigData 获取线路与站点",
                min(start + len(batch), total),
                max(total, 1),
            )
            self._fetch_batch_recursive(tbd, batch, city_code)
            self._emit_metrics()

        self._check_cancel()
        self.db.set_meta("task_state", "POSTPROCESSING")
        self.log("正在按站名 + 空间距离合并物理公交站点…")
        if self.db.conn.execute("SELECT COUNT(*) FROM raw_stops").fetchone()[0] > 0:
            rebuild_station_groups_tracked(self.db, self.merge_radius_m)

        final_failures = 0
        for table in ("tbd_broad_probe", "tbd_probe", "tbd_fetch_state"):
            try:
                final_failures += int(
                    self.db.conn.execute(
                        f"SELECT COUNT(*) FROM {table} WHERE status='failed_final'"
                    ).fetchone()[0]
                )
            except Exception:
                pass

        if final_failures:
            self.db.set_meta("completed", "0")
            self.db.set_meta("task_state", "PARTIAL")
            self._emit_metrics()
            raise RuntimeError(
                f"采集已完成可恢复部分，但仍有 {final_failures} 个任务在多次重试后失败。"
                "数据已保存，可再次运行或检查失败项后继续。"
            )

        self.db.set_meta("completed", "1")
        self.db.set_meta("task_state", "COMPLETED")
        self._emit_metrics()
        self.log("TransBigData 采集完成，可导出 CSV / Excel / GPKG / SHP 并继续 GIS 分析。")


_backend.BaiduLineDiscoverer = ResumableBaiduLineDiscoverer
_backend.TransBigDataCollector = ReliableTransBigDataCollector

DiscoveryConfig = _backend.DiscoveryConfig
BaiduLineDiscoverer = ResumableBaiduLineDiscoverer
TransBigDataCollector = ReliableTransBigDataCollector
