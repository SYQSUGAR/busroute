from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
)

from transit_collector.db import TransitDB
from transit_collector.exporter import Exporter
from transit_collector.project import ProjectManager, TaskState
from transit_collector.ui.transbigdata_window import TransitWorkbenchWindow, APP_NAME
from transit_collector.workflow import (
    database_summary,
    is_partial_database,
    rebuild_station_groups_tracked,
    source_signature,
    station_dependency_state,
)


class WorkflowWorkbenchWindow(TransitWorkbenchWindow):
    """v0.4 task-oriented shell around the existing collector/GIS engine."""

    def __init__(self):
        self.current_project: ProjectManager | None = None
        self.active_run_id: str | None = None
        self._task_running = False
        self._last_source: str | None = None
        super().__init__()
        self.setWindowTitle("公交数据采集与 GIS 分析工作台 · v0.4")
        self._install_project_bar()
        self.gis_db_edit.setReadOnly(True)
        self.scope_search.textEdited.connect(self._scope_text_edited)
        self.merge_radius.valueChanged.connect(self._update_dependency_status)
        self._restore_recent_project()
        self._update_project_bar()

    def _install_project_bar(self):
        card = QFrame()
        card.setObjectName("Card")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)

        self.project_label = QLabel("当前项目：未打开")
        self.project_label.setStyleSheet("font-weight:700;color:#0f172a;")
        self.project_state_label = QLabel("状态：未配置")
        self.project_dependency_label = QLabel("站点数据：—")
        self.project_dependency_label.setStyleSheet("color:#64748b;")

        self.open_project_btn = QPushButton("打开项目 / 数据库")
        self.open_project_btn.clicked.connect(self.open_existing_project)
        self.new_task_btn = QPushButton("新建任务")
        self.new_task_btn.clicked.connect(self.new_task)
        self.rebuild_stations_btn = QPushButton("重新整理站点")
        self.rebuild_stations_btn.clicked.connect(self.rebuild_stations_now)
        self.rebuild_stations_btn.setEnabled(False)

        row.addWidget(self.project_label, 2)
        row.addWidget(self.project_state_label)
        row.addWidget(self.project_dependency_label, 2)
        row.addStretch()
        row.addWidget(self.rebuild_stations_btn)
        row.addWidget(self.open_project_btn)
        row.addWidget(self.new_task_btn)

        self.centralWidget().layout().insertWidget(1, card)

    def _source_changed(self):
        previous = getattr(self, "_last_source", None)
        super()._source_changed()
        if not hasattr(self, "source_combo"):
            return
        current = self.source_combo.currentData()
        if previous is not None and current != previous and not getattr(self, "_task_running", False):
            self._invalidate_scope("数据源已切换，请重新确认采集范围。")
        self._last_source = current

    def _scope_text_edited(self, _text: str):
        if self._task_running:
            return
        if self.selected_scope is not None:
            self.selected_scope = None
            self.search_results = []
            self.scope_result.blockSignals(True)
            self.scope_result.clear()
            self.scope_result.blockSignals(False)
            self.selected_label.setText("搜索条件已修改，请重新确认范围")

    def _invalidate_scope(self, message: str = "尚未选择"):
        self.selected_scope = None
        self.search_results = []
        if hasattr(self, "scope_result"):
            self.scope_result.blockSignals(True)
            self.scope_result.clear()
            self.scope_result.blockSignals(False)
        if hasattr(self, "selected_label"):
            self.selected_label.setText(message)

    def _lock_task_controls(self, locked: bool):
        self._task_running = locked
        widgets = [
            self.source_combo,
            self.scan_combo,
            self.extra_keywords_edit,
            self.scope_mode,
            self.scope_search,
            self.search_btn,
            self.key_edit,
            self.remember_key,
            self.test_btn,
            self.interval_spin,
            self.merge_radius,
            self.output_edit,
            self.output_btn,
            self.open_project_btn,
            self.new_task_btn,
            self.rebuild_stations_btn,
        ]
        for widget in widgets:
            widget.setEnabled(not locked)
        if not locked:
            self._source_changed()
            self.rebuild_stations_btn.setEnabled(bool(self.last_db_path and self.last_db_path.exists()))
        self._set_gis_busy(False)

    def _set_gis_busy(self, busy: bool):
        # IntegratedMainWindow calls this after GIS tasks. Collection lock wins.
        self.base_gis_btn.setEnabled(not busy and not self._task_running)
        self.coverage_btn.setEnabled(not busy and not self._task_running)

    def _project_params(self) -> dict:
        return {
            "source": self.source_combo.currentData(),
            "city_or_scope": self.selected_scope.name if self.selected_scope else self.scope_search.text().strip(),
            "scope_level": self.scope_mode.currentData(),
            "scan_mode": self.scan_combo.currentData(),
            "extra_keywords": self.extra_keywords_edit.text().strip(),
            "request_interval_s": float(self.interval_spin.value()),
            "merge_radius_m": float(self.merge_radius.value()),
            "output_dir": self.output_edit.text().strip(),
        }

    def _source_display(self) -> str:
        return "TransBigData/Baidu" if self._source_is_tbd() else "AMap Web Service"

    def _ensure_project_for_current_task(self, db_path: Path) -> ProjectManager:
        city = self.selected_scope.name if self.selected_scope else self.scope_search.text().strip()
        project = ProjectManager.for_database(
            db_path,
            name=f"{city}公交数据" if city else db_path.stem,
            city=city,
            source=self._source_display(),
        )
        project.manifest.merge_radius_m = float(self.merge_radius.value())
        project.save()
        self.current_project = project
        self.settings.setValue("recent_project_db", str(db_path.resolve()))
        self._update_project_bar()
        return project

    def start_crawl(self):
        if self.crawl_thread and self.crawl_thread.isRunning():
            return

        # Validate before locking the form. The parent implementation will repeat
        # the checks, but doing them here prevents creating phantom projects.
        if self._source_is_tbd():
            city = self.scope_search.text().strip()
            if not city:
                QMessageBox.warning(self, "缺少城市", "请输入城市名称。")
                return
            if not self.selected_scope or self.selected_scope.name != city:
                self.search_scope()
        else:
            if not self.key_edit.text().strip():
                QMessageBox.warning(self, "缺少 Key", "请先输入高德 Web 服务 API Key。")
                return
            if not self.selected_scope:
                QMessageBox.warning(self, "未选择范围", "请重新搜索并选择城市或省域。")
                return
        if not self.output_edit.text().strip():
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return

        db_path = self._db_path_for_scope()
        project = self._ensure_project_for_current_task(db_path)
        state = TaskState.DISCOVERING if self._source_is_tbd() else TaskState.CRAWLING
        project.mark_state(state, data_status="partial")
        self.active_run_id, _ = project.start_run("collect", self._project_params())
        self._lock_task_controls(True)
        self._update_project_bar()
        try:
            super().start_crawl()
        except Exception as exc:
            project.finish_run("failed", str(exc))
            project.mark_state(TaskState.FAILED, data_status="partial")
            self._lock_task_controls(False)
            self._update_project_bar()
            raise

    def stop_crawl(self):
        if self.current_project and self.crawl_thread and self.crawl_thread.isRunning():
            self.current_project.mark_state(TaskState.PAUSING, data_status="partial")
            self.current_project.append_log("用户请求安全停止任务。")
            self._update_project_bar()
        super().stop_crawl()

    def append_log(self, text):
        super().append_log(text)
        if self.current_project and self.active_run_id:
            try:
                self.current_project.append_log(str(text))
            except OSError:
                pass

    def crawl_done(self, db_path):
        self._lock_task_controls(False)
        super().crawl_done(db_path)
        self._adopt_database(Path(db_path), refresh=False)
        if self.current_project:
            self.current_project.finish_run("completed", "采集和站点后处理完成。")
            self.current_project.mark_state(TaskState.COMPLETED, data_status="complete")
            self.current_project.manifest.merge_radius_m = float(self.merge_radius.value())
            self.current_project.save()
        self.active_run_id = None
        self._update_project_bar()
        self._update_dependency_status()

    def crawl_failed(self, msg):
        self._lock_task_controls(False)
        super().crawl_failed(msg)
        state = TaskState.PAUSED if "停止" in str(msg) else TaskState.PARTIAL
        if self.current_project:
            self.current_project.finish_run("paused" if state == TaskState.PAUSED else "partial", str(msg))
            self.current_project.mark_state(state, data_status="partial")
        self.active_run_id = None
        self._update_project_bar()
        self._update_dependency_status()

    def _restore_recent_project(self):
        value = str(self.settings.value("recent_project_db", "") or "")
        if value:
            path = Path(value)
            if path.exists():
                try:
                    self._adopt_database(path, refresh=False)
                except Exception:
                    pass

    def _adopt_database(self, db_path: Path, *, refresh: bool = True):
        db_path = Path(db_path).resolve()
        db = TransitDB(db_path)
        try:
            summary = database_summary(db)
            source = str(summary.get("data_source") or "")
            scope_raw = db.get_meta("scope", "")
            city = ""
            if scope_raw:
                try:
                    scope = json.loads(scope_raw) if isinstance(scope_raw, str) else scope_raw
                    if isinstance(scope, dict):
                        city = str(scope.get("name", ""))
                except Exception:
                    pass
            task_state = str(db.get_meta("task_state", "") or "")
            if task_state not in TaskState.LABELS:
                task_state = TaskState.COMPLETED if summary["completed"] else TaskState.PAUSED
        finally:
            db.close()

        self.current_project = ProjectManager.for_database(
            db_path,
            name=f"{city}公交数据" if city else db_path.stem,
            city=city,
            source=source,
        )
        self.current_project.mark_state(
            task_state,
            data_status="complete" if summary["completed"] else "partial",
        )
        self.last_db_path = db_path
        self.gis_db_edit.setText(str(db_path))
        self.export_btn.setEnabled(True)
        self.rebuild_stations_btn.setEnabled(not self._task_running)
        self.settings.setValue("recent_project_db", str(db_path))
        if refresh:
            self.refresh_preview()
        self._update_project_bar()
        self._update_dependency_status()

    def open_existing_project(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "打开已有项目或公交数据库",
            self.output_edit.text().strip() or str(Path.home()),
            "Transit project/database (project.json *.sqlite *.db);;SQLite (*.sqlite *.db);;Project manifest (project.json);;All files (*.*)",
        )
        if not path:
            return
        p = Path(path)
        try:
            if p.name == "project.json":
                manager = ProjectManager.open_manifest(p)
                self.current_project = manager
                self._adopt_database(manager.database_path)
            else:
                self._adopt_database(p)
        except Exception as exc:
            QMessageBox.critical(self, "打开失败", str(exc))
            return
        QMessageBox.information(self, "已打开", f"当前数据库：\n{self.last_db_path}")

    def browse_gis_db(self):
        # Opening a database from GIS changes the global project context as well,
        # preventing main-page and GIS-page operations from pointing at different DBs.
        self.open_existing_project()

    def new_task(self):
        if self._task_running:
            return
        self.current_project = None
        self.last_db_path = None
        self.gis_db_edit.clear()
        self.export_btn.setEnabled(False)
        self.rebuild_stations_btn.setEnabled(False)
        self._invalidate_scope("尚未选择")
        self.lines_table.setRowCount(0)
        self.stops_table.setRowCount(0)
        self.preview.draw_points([])
        self.project_label.setText("当前项目：新任务")
        self.project_state_label.setText("状态：未配置")
        self.project_dependency_label.setText("站点数据：—")

    def _update_project_bar(self):
        if not hasattr(self, "project_label"):
            return
        if not self.current_project:
            self.project_label.setText("当前项目：未打开")
            self.project_state_label.setText("状态：未配置")
            return
        m = self.current_project.manifest
        self.project_label.setText(
            f"当前项目：{m.name}  ｜  {m.source or '未知数据源'}  ｜  {Path(m.database).name}"
        )
        self.project_state_label.setText(f"状态：{TaskState.LABELS.get(m.state, m.state)}")

    def _update_dependency_status(self, *_args):
        if not hasattr(self, "project_dependency_label"):
            return
        db_path = self.last_db_path
        if not db_path or not db_path.exists():
            self.project_dependency_label.setText("站点数据：—")
            return
        db = TransitDB(db_path)
        try:
            state = station_dependency_state(db, float(self.merge_radius.value()))
        finally:
            db.close()
        if state.current:
            self.project_dependency_label.setText(
                f"站点数据：✓ 最新（{state.station_count}个，{state.desired_radius_m:g}m）"
            )
            self.project_dependency_label.setStyleSheet("color:#15803d;font-weight:600;")
        else:
            self.project_dependency_label.setText(f"站点数据：⚠ 需更新｜{state.reason}")
            self.project_dependency_label.setStyleSheet("color:#b45309;font-weight:600;")

    def _ensure_station_dependency(self, db_path: Path, purpose: str, *, force: bool = False) -> bool:
        if self._task_running:
            QMessageBox.warning(self, "采集正在进行", "正式导出/GIS分析需等待采集完成或先安全暂停任务。")
            return False
        db = TransitDB(db_path)
        try:
            partial = is_partial_database(db)
            if partial and not force:
                reply = QMessageBox.question(
                    self,
                    "当前为部分数据",
                    f"当前数据库尚未完整采集。\n\n继续“{purpose}”将基于当前快照，不能视为完整成果。\n\n是否继续？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return False

            state = station_dependency_state(db, float(self.merge_radius.value()))
            if state.current and not force:
                return True
            if not force:
                reply = QMessageBox.question(
                    self,
                    "需要更新合并站点",
                    f"{state.reason}\n\n系统需要先按 {self.merge_radius.value()}m 重新整理站点，再继续“{purpose}”。\n是否更新？",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes,
                )
                if reply != QMessageBox.Yes:
                    return False
            if self.current_project and self.current_project.database_path.resolve() == db_path.resolve():
                self.current_project.backup_database("before_station_merge")
            rebuild_station_groups_tracked(db, float(self.merge_radius.value()))
            if self.current_project and self.current_project.database_path.resolve() == db_path.resolve():
                self.current_project.manifest.merge_radius_m = float(self.merge_radius.value())
                self.current_project.save()
        finally:
            db.close()
        self.refresh_preview()
        self._update_dependency_status()
        return True

    def rebuild_stations_now(self):
        if not self.last_db_path or not self.last_db_path.exists():
            QMessageBox.warning(self, "没有数据库", "请先打开或采集一个公交数据库。")
            return
        QApplication.setOverrideCursor(Qt.WaitCursor) if False else None
        try:
            if self._ensure_station_dependency(self.last_db_path, "重新整理站点", force=True):
                QMessageBox.information(self, "完成", f"已按 {self.merge_radius.value()}m 重新整理合并站点。")
        except Exception as exc:
            QMessageBox.critical(self, "站点整理失败", str(exc))

    def export_results(self):
        path = self.last_db_path or (self._db_path_for_scope() if self.selected_scope else None)
        if not path or not Path(path).exists():
            QMessageBox.warning(self, "无数据库", "尚未找到可导出的公交数据库。")
            return
        path = Path(path)
        if not self._ensure_station_dependency(path, "导出成果"):
            return
        export_dir = path.parent / (path.stem + "_export")
        db = TransitDB(path)
        try:
            sig = source_signature(db)
            files = Exporter(db).export_all(export_dir)
        finally:
            db.close()
        self.refresh_preview()
        if self.current_project and self.current_project.database_path.resolve() == path.resolve():
            for kind, output in files.items():
                self.current_project.register_artifact(
                    f"export:{kind}", output, parameters={"merge_radius_m": float(self.merge_radius.value())}, source_signature=sig
                )
        QMessageBox.information(self, "导出完成", f"已导出到：\n{export_dir}")
        self.append_log("导出文件：\n" + "\n".join(files.values()))

    def export_gis_layers(self):
        db_path = self._selected_db_path()
        if not db_path:
            QMessageBox.warning(self, "缺少数据库", "请选择已有的公交 SQLite 数据库。")
            return
        if not self._ensure_station_dependency(db_path, "生成 GIS 基础图层"):
            return
        super().export_gis_layers()

    def run_coverage_analysis(self):
        db_path = self._selected_db_path()
        if not db_path:
            QMessageBox.warning(self, "缺少数据库", "请选择已有的公交 SQLite 数据库。")
            return
        if not self._ensure_station_dependency(db_path, "公交站服务覆盖分析"):
            return
        super().run_coverage_analysis()

    def _gis_export_done(self, result):
        super()._gis_export_done(result)
        if self.current_project:
            db_path = self._selected_db_path()
            if db_path and db_path.resolve() == self.current_project.database_path.resolve():
                db = TransitDB(db_path)
                try:
                    sig = source_signature(db)
                finally:
                    db.close()
                for kind, path in result.items():
                    self.current_project.register_artifact(
                        f"gis:{kind}", path, parameters={"merge_radius_m": float(self.merge_radius.value())}, source_signature=sig
                    )

    def _coverage_done(self, result):
        super()._coverage_done(result)
        if self.current_project:
            for key in ("gpkg", "summary_json", "zones_shp", "service_shp", "covered_shp", "uncovered_shp"):
                path = result.get(key)
                if path:
                    self.current_project.register_artifact(
                        f"analysis:coverage:{key}",
                        path,
                        parameters={
                            "buffer_m": result.get("buffer_m"),
                            "metric_crs": result.get("metric_crs"),
                        },
                    )

    def closeEvent(self, event):
        if self.crawl_thread and self.crawl_thread.isRunning():
            reply = QMessageBox.question(
                self,
                "任务仍在运行",
                "当前采集任务仍在运行。是否安全停止并退出？\n\n程序会停止提交新请求，并等待当前请求结束后再关闭数据库。",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                event.ignore()
                return
            if self.current_project:
                self.current_project.mark_state(TaskState.PAUSING, data_status="partial")
                self.current_project.append_log("用户关闭程序：开始安全停止。")
            self.crawl_thread.stop()
            if not self.crawl_thread.wait(20000):
                QMessageBox.warning(
                    self,
                    "仍在等待网络请求",
                    "当前请求尚未安全结束。为避免数据库状态不一致，本次暂不退出。请稍后再次关闭。",
                )
                event.ignore()
                return
            if self.current_project:
                self.current_project.mark_state(TaskState.PAUSED, data_status="partial")
                self.current_project.finish_run("paused", "程序关闭时安全暂停。")
        self._save_settings()
        QMainWindow.closeEvent(self, event)


def run_app():
    app = QApplication.instance() or QApplication([])
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = WorkflowWorkbenchWindow()
    win.show()
    return app.exec()
