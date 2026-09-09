from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from transit_collector.db import TransitDB
from transit_collector.gis import GISProcessor
from transit_collector.ui.main_window import APP_NAME, MainWindow


class GISAnalysisThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        db_path: Path,
        boundary_path: Path,
        out_dir: Path,
        buffer_m: float,
        boundary_layer: str | None,
        metric_crs: str | None,
        write_shp: bool,
    ):
        super().__init__()
        self.db_path = db_path
        self.boundary_path = boundary_path
        self.out_dir = out_dir
        self.buffer_m = buffer_m
        self.boundary_layer = boundary_layer
        self.metric_crs = metric_crs
        self.write_shp = write_shp

    def run(self):
        db = None
        try:
            db = TransitDB(self.db_path)
            result = GISProcessor(db).analyze_station_coverage(
                boundary_path=self.boundary_path,
                out_dir=self.out_dir,
                buffer_m=self.buffer_m,
                boundary_layer=self.boundary_layer,
                metric_crs=self.metric_crs,
                write_shp=self.write_shp,
            )
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if db:
                db.close()


class GISExportThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, db_path: Path, out_dir: Path, write_shp: bool):
        super().__init__()
        self.db_path = db_path
        self.out_dir = out_dir
        self.write_shp = write_shp

    def run(self):
        db = None
        try:
            db = TransitDB(self.db_path)
            result = GISProcessor(db).export_base_layers(self.out_dir, write_shp=self.write_shp)
            self.done.emit(result)
        except Exception as e:
            self.failed.emit(str(e))
        finally:
            if db:
                db.close()


class IntegratedMainWindow(MainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("公交线路与站点 GIS 采集分析器 · 高德 Web 服务")
        self.export_btn.setText("导出 CSV / Excel / GIS")
        self.gis_thread = None
        self._add_gis_tab()

    def _add_gis_tab(self):
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(18, 18, 18, 18)
        outer.setSpacing(12)

        title = QLabel("GIS 矢量建库与公交站服务覆盖分析")
        title.setStyleSheet("font-size:18px;font-weight:700;")
        desc = QLabel(
            "基础矢量使用 GeoPandas + Shapely + pyogrio。高德 GCJ-02 坐标会先近似反算为 "
            "WGS84，再生成标准 GIS 图层；500m 等距离/面积分析在自动选择的本地 UTM 米制投影中完成。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color:#64748b;")
        outer.addWidget(title)
        outer.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(10)

        db_row = QHBoxLayout()
        self.gis_db_edit = QLineEdit()
        self.gis_db_edit.setPlaceholderText("选择 transit_<adcode>.sqlite，或使用当前采集数据库")
        use_current_btn = QPushButton("使用当前")
        use_current_btn.clicked.connect(self.use_current_db)
        browse_db_btn = QPushButton("浏览")
        browse_db_btn.clicked.connect(self.browse_gis_db)
        db_row.addWidget(self.gis_db_edit, 1)
        db_row.addWidget(use_current_btn)
        db_row.addWidget(browse_db_btn)
        form.addRow("公交数据库", db_row)

        boundary_row = QHBoxLayout()
        self.boundary_edit = QLineEdit()
        self.boundary_edit.setPlaceholderText("建成区 / 居住用地 / 街区 / 社区等 Polygon 图层")
        boundary_btn = QPushButton("浏览")
        boundary_btn.clicked.connect(self.browse_boundary)
        boundary_row.addWidget(self.boundary_edit, 1)
        boundary_row.addWidget(boundary_btn)
        form.addRow("分析范围", boundary_row)

        self.boundary_layer_edit = QLineEdit()
        self.boundary_layer_edit.setPlaceholderText("GPKG 多图层时可填写 layer 名；SHP/GeoJSON 留空")
        form.addRow("图层名称", self.boundary_layer_edit)

        self.buffer_spin = QSpinBox()
        self.buffer_spin.setRange(50, 5000)
        self.buffer_spin.setValue(500)
        self.buffer_spin.setSuffix(" m")
        form.addRow("服务半径", self.buffer_spin)

        self.metric_crs_edit = QLineEdit()
        self.metric_crs_edit.setPlaceholderText("留空=自动估算 UTM；也可填 EPSG:32649 等米制投影")
        form.addRow("分析 CRS", self.metric_crs_edit)

        out_row = QHBoxLayout()
        self.gis_out_edit = QLineEdit()
        self.gis_out_edit.setPlaceholderText("留空则在数据库旁自动创建 GIS 输出目录")
        out_btn = QPushButton("浏览")
        out_btn.clicked.connect(self.browse_gis_output)
        out_row.addWidget(self.gis_out_edit, 1)
        out_row.addWidget(out_btn)
        form.addRow("GIS 输出目录", out_row)

        self.write_shp_check = QCheckBox("同时输出 Shapefile（主格式仍为 GeoPackage）")
        self.write_shp_check.setChecked(True)
        form.addRow("", self.write_shp_check)

        outer.addLayout(form)

        btn_row = QHBoxLayout()
        self.base_gis_btn = QPushButton("生成 GPKG / SHP 基础图层")
        self.base_gis_btn.clicked.connect(self.export_gis_layers)
        self.coverage_btn = QPushButton("计算公交站服务覆盖率")
        self.coverage_btn.setObjectName("Primary")
        self.coverage_btn.clicked.connect(self.run_coverage_analysis)
        btn_row.addWidget(self.base_gis_btn)
        btn_row.addWidget(self.coverage_btn, 1)
        outer.addLayout(btn_row)

        self.gis_result = QLabel("尚未运行分析。")
        self.gis_result.setWordWrap(True)
        self.gis_result.setStyleSheet(
            "padding:10px;background:#f8fafc;border-radius:8px;color:#334155;font-weight:600;"
        )
        outer.addWidget(self.gis_result)

        self.zone_table = QTableWidget(0, 5)
        self.zone_table.setHorizontalHeaderLabels(["分区", "行号", "总面积(m²)", "覆盖面积(m²)", "覆盖率(%)"])
        self.zone_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.zone_table.horizontalHeader().setStretchLastSection(True)
        self.zone_table.setAlternatingRowColors(True)
        outer.addWidget(self.zone_table, 1)

        self.gis_log = QTextEdit()
        self.gis_log.setReadOnly(True)
        self.gis_log.setMaximumHeight(130)
        outer.addWidget(self.gis_log)

        self.tabs.addTab(tab, "GIS分析")

    def crawl_done(self, db_path):
        super().crawl_done(db_path)
        self.gis_db_edit.setText(str(db_path))

    def use_current_db(self):
        path = self.last_db_path
        if path and path.exists():
            self.gis_db_edit.setText(str(path))
            return
        if self.selected_scope:
            candidate = self._db_path_for_scope()
            if candidate.exists():
                self.gis_db_edit.setText(str(candidate))
                return
        QMessageBox.information(self, "没有当前数据库", "请先完成/继续一次采集，或手动选择 SQLite 数据库。")

    def browse_gis_db(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择公交 SQLite 数据库",
            self.output_edit.text().strip() or str(Path.home()),
            "SQLite (*.sqlite *.db);;All files (*.*)",
        )
        if path:
            self.gis_db_edit.setText(path)

    def browse_boundary(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择分析范围矢量",
            str(Path.home()),
            "Vector (*.shp *.gpkg *.geojson *.json);;Shapefile (*.shp);;GeoPackage (*.gpkg);;All files (*.*)",
        )
        if path:
            self.boundary_edit.setText(path)

    def browse_gis_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择 GIS 输出目录", self._suggest_gis_output().as_posix())
        if path:
            self.gis_out_edit.setText(path)

    def _selected_db_path(self) -> Path | None:
        text = self.gis_db_edit.text().strip()
        if text:
            p = Path(text)
            if p.exists():
                return p
        if self.last_db_path and self.last_db_path.exists():
            return self.last_db_path
        return None

    def _suggest_gis_output(self) -> Path:
        db_path = self._selected_db_path()
        if db_path:
            return db_path.parent / f"{db_path.stem}_gis"
        return Path(self.output_edit.text().strip() or Path.home()) / "transit_gis"

    def _analysis_output(self) -> Path:
        text = self.gis_out_edit.text().strip()
        base = Path(text) if text else self._suggest_gis_output()
        return base / f"coverage_{self.buffer_spin.value()}m"

    def _set_gis_busy(self, busy: bool):
        self.base_gis_btn.setEnabled(not busy)
        self.coverage_btn.setEnabled(not busy)

    def export_gis_layers(self):
        db_path = self._selected_db_path()
        if not db_path:
            QMessageBox.warning(self, "缺少数据库", "请选择已有的公交 SQLite 数据库。")
            return
        out = Path(self.gis_out_edit.text().strip()) if self.gis_out_edit.text().strip() else self._suggest_gis_output()
        out.mkdir(parents=True, exist_ok=True)
        self.gis_out_edit.setText(str(out))
        self._set_gis_busy(True)
        self.gis_log.append(f"正在生成基础 GIS 图层：{out}")
        self.gis_thread = GISExportThread(db_path, out, self.write_shp_check.isChecked())
        self.gis_thread.done.connect(self._gis_export_done)
        self.gis_thread.failed.connect(self._gis_failed)
        self.gis_thread.start()

    def _gis_export_done(self, result):
        self._set_gis_busy(False)
        self.gis_result.setText(
            "基础 GIS 图层已生成。主格式为 GeoPackage；若勾选兼容输出，同时生成 SHP。"
        )
        self.gis_log.append("\n".join(f"{k}: {v}" for k, v in result.items()))
        QMessageBox.information(self, "GIS 导出完成", f"已生成 {len(result)} 项 GIS 输出。")

    def run_coverage_analysis(self):
        db_path = self._selected_db_path()
        boundary = Path(self.boundary_edit.text().strip())
        if not db_path:
            QMessageBox.warning(self, "缺少数据库", "请选择已有的公交 SQLite 数据库。")
            return
        if not boundary.exists():
            QMessageBox.warning(self, "缺少分析范围", "请选择有效的 Polygon / MultiPolygon 矢量文件。")
            return

        out = self._analysis_output()
        out.mkdir(parents=True, exist_ok=True)
        layer = self.boundary_layer_edit.text().strip() or None
        metric_crs = self.metric_crs_edit.text().strip() or None

        self._set_gis_busy(True)
        self.gis_result.setText("正在进行空间投影、Buffer、Union、Intersection 与面积统计…")
        self.gis_log.append(
            f"分析：DB={db_path}；范围={boundary}；半径={self.buffer_spin.value()}m；输出={out}"
        )

        self.gis_thread = GISAnalysisThread(
            db_path=db_path,
            boundary_path=boundary,
            out_dir=out,
            buffer_m=float(self.buffer_spin.value()),
            boundary_layer=layer,
            metric_crs=metric_crs,
            write_shp=self.write_shp_check.isChecked(),
        )
        self.gis_thread.done.connect(self._coverage_done)
        self.gis_thread.failed.connect(self._gis_failed)
        self.gis_thread.start()

    def _coverage_done(self, result):
        self._set_gis_busy(False)
        self.gis_result.setText(
            f"覆盖率：{result['coverage_pct']:.2f}% ｜ "
            f"覆盖面积：{result['covered_area_m2'] / 1_000_000:.3f} km² ｜ "
            f"分析范围：{result['total_area_m2'] / 1_000_000:.3f} km² ｜ "
            f"参与站点：{result['station_count']} ｜ 投影：{result['metric_crs']}"
        )

        stats = result.get("zone_stats", [])
        self.zone_table.setRowCount(len(stats))
        for r, item in enumerate(stats):
            values = [
                item.get("label", ""),
                item.get("row_id", ""),
                f"{item.get('area_m2', 0):.2f}",
                f"{item.get('covered_m2', 0):.2f}",
                f"{item.get('coverage_pct', 0):.2f}",
            ]
            for c, value in enumerate(values):
                self.zone_table.setItem(r, c, QTableWidgetItem(str(value)))

        self.gis_log.append(f"分析完成：{result.get('gpkg', '')}")
        if result.get("summary_json"):
            self.gis_log.append(f"统计摘要：{result['summary_json']}")
        QMessageBox.information(
            self,
            "覆盖率分析完成",
            f"公交站 {result['buffer_m']:.0f}m 服务覆盖率：{result['coverage_pct']:.2f}%",
        )

    def _gis_failed(self, message):
        self._set_gis_busy(False)
        self.gis_result.setText("GIS 操作失败。")
        self.gis_log.append(f"失败：{message}")
        QMessageBox.critical(self, "GIS 操作失败", message)


def run_app():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = IntegratedMainWindow()
    win.show()
    sys.exit(app.exec())
