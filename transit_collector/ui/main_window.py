from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

import keyring
from PySide6.QtCore import Qt, QThread, Signal, QSettings
from PySide6.QtGui import QPainter
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame, QGraphicsScene,
    QGraphicsView, QGridLayout, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPushButton, QProgressBar, QSpinBox, QDoubleSpinBox, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget
)

from transit_collector.api.amap import AMapClient, AMapError, District
from transit_collector.crawler import Scope, TransitCrawler, MUNICIPALITIES
from transit_collector.db import TransitDB
from transit_collector.exporter import Exporter


APP_NAME = "AMapTransitCollector"
KEYRING_SERVICE = "AMapTransitCollector"


APP_QSS = """
QWidget {
    font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif;
    font-size: 13px;
    color: #1f2937;
}
QMainWindow { background: #f4f7fb; }
QFrame#Card {
    background: white;
    border: 1px solid #e5eaf1;
    border-radius: 12px;
}
QLabel#Title { font-size: 22px; font-weight: 700; color: #0f172a; }
QLabel#SubTitle { color: #64748b; }
QLabel#MetricNumber { font-size: 23px; font-weight: 700; color: #0f172a; }
QLabel#MetricLabel { color: #64748b; font-size: 12px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    min-height: 34px;
    border: 1px solid #d7dee8;
    border-radius: 8px;
    padding: 0 10px;
    background: white;
}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus {
    border: 1px solid #2563eb;
}
QPushButton {
    min-height: 34px;
    border: none;
    border-radius: 8px;
    padding: 0 14px;
    background: #e8eef8;
    color: #1e3a5f;
    font-weight: 600;
}
QPushButton:hover { background: #dbe7f7; }
QPushButton#Primary {
    background: #2563eb;
    color: white;
}
QPushButton#Primary:hover { background: #1d4ed8; }
QPushButton#Danger {
    background: #fee2e2;
    color: #991b1b;
}
QPushButton:disabled { color: #9ca3af; background: #eef2f7; }
QTabWidget::pane {
    border: 1px solid #e5eaf1;
    background: white;
    border-radius: 8px;
}
QTabBar::tab {
    padding: 9px 16px;
    margin-right: 3px;
    border-radius: 7px;
}
QTabBar::tab:selected { background: #e8f0ff; color: #1d4ed8; }
QTableWidget {
    border: none;
    gridline-color: #edf0f4;
    background: white;
    alternate-background-color: #f8fafc;
}
QHeaderView::section {
    background: #f8fafc;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 8px;
    font-weight: 600;
}
QProgressBar {
    border: 1px solid #dbe2ea;
    border-radius: 7px;
    background: #eef2f7;
    text-align: center;
    min-height: 20px;
}
QProgressBar::chunk {
    border-radius: 6px;
    background: #3b82f6;
}
"""


class DistrictSearchThread(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, key: str, keyword: str, mode: str, interval: float):
        super().__init__()
        self.key = key
        self.keyword = keyword
        self.mode = mode
        self.interval = interval

    def run(self):
        try:
            client = AMapClient(self.key, min_interval=self.interval)
            items = client.search_district(self.keyword, subdistrict=0, extensions="base")
            filtered = []
            for d in items:
                if self.mode == "city":
                    if d.level == "city" or (d.level == "province" and d.name in MUNICIPALITIES):
                        filtered.append(d)
                else:
                    if d.level == "province":
                        filtered.append(d)
            self.done.emit(filtered)
        except Exception as e:
            self.failed.emit(str(e))


class CrawlThread(QThread):
    log_sig = Signal(str)
    progress_sig = Signal(str, int, int)
    metrics_sig = Signal(dict)
    done_sig = Signal(str)
    failed_sig = Signal(str)

    def __init__(self, key: str, scope: Scope, db_path: Path, interval: float, merge_radius: float):
        super().__init__()
        self.key = key
        self.scope = scope
        self.db_path = db_path
        self.interval = interval
        self.merge_radius = merge_radius
        self.cancel_event = threading.Event()
        self.api_calls = 0

    def stop(self):
        self.cancel_event.set()

    def run(self):
        db = None
        try:
            def on_req(_endpoint):
                self.api_calls += 1
                if self.api_calls % 5 == 0:
                    self.emit_metrics(db)

            client = AMapClient(
                self.key,
                min_interval=self.interval,
                on_request=on_req,
            )
            db = TransitDB(self.db_path)

            def emit_metrics():
                self.emit_metrics(db)

            crawler = TransitCrawler(
                client=client,
                db=db,
                scope=self.scope,
                merge_radius_m=self.merge_radius,
                cancel_event=self.cancel_event,
                log=self.log_sig.emit,
                progress=self.progress_sig.emit,
                metrics=emit_metrics,
            )
            crawler.run()
            self.emit_metrics(db)
            self.done_sig.emit(str(self.db_path))
        except InterruptedError as e:
            self.log_sig.emit(str(e))
            if db:
                self.emit_metrics(db)
            self.failed_sig.emit("采集已停止，进度已保存在数据库中，可再次开始继续采集。")
        except AMapError as e:
            if e.is_quota_error:
                msg = f"API 配额/频率受限：{e}\n当前进度已保存，配额恢复后可直接继续。"
            else:
                msg = str(e)
            self.failed_sig.emit(msg)
        except Exception:
            self.failed_sig.emit(traceback.format_exc())
        finally:
            if db:
                db.close()

    def emit_metrics(self, db):
        if not db:
            return
        m = db.counts()
        m["api_calls"] = self.api_calls
        self.metrics_sig.emit(m)


class GeometryPreview(QGraphicsView):
    def __init__(self):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.setRenderHint(QPainter.Antialiasing, True)
        self.setMinimumHeight(280)

    def draw_points(self, rows):
        scene = self.scene()
        scene.clear()
        pts = [(r["longitude"], r["latitude"], r["name"]) for r in rows if r["longitude"] is not None and r["latitude"] is not None]
        if not pts:
            scene.addText("暂无可预览坐标")
            return
        minx, maxx = min(p[0] for p in pts), max(p[0] for p in pts)
        miny, maxy = min(p[1] for p in pts), max(p[1] for p in pts)
        w, h = 900, 550
        sx = w / max(maxx - minx, 1e-9)
        sy = h / max(maxy - miny, 1e-9)
        scale = min(sx, sy)
        for x, y, name in pts[:5000]:
            px = (x - minx) * scale
            py = h - (y - miny) * scale
            scene.addEllipse(px - 1.8, py - 1.8, 3.6, 3.6)
        scene.setSceneRect(0, 0, w, h)
        self.fitInView(scene.sceneRect(), Qt.KeepAspectRatio)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("OpenAI", APP_NAME)
        self.selected_scope: Scope | None = None
        self.search_results: list[District] = []
        self.search_thread = None
        self.crawl_thread = None
        self.last_db_path: Path | None = None

        self.setWindowTitle("公交线路与站点采集器 · 高德 Web 服务")
        self.resize(1320, 840)
        self.setStyleSheet(APP_QSS)
        self._build_ui()
        self._restore_settings()

    def _card(self):
        f = QFrame()
        f.setObjectName("Card")
        return f

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 18)
        outer.setSpacing(12)

        title_row = QHBoxLayout()
        title_box = QVBoxLayout()
        title = QLabel("公交线路与站点采集器")
        title.setObjectName("Title")
        subtitle = QLabel("官方 API · 行政区限定 · 断点续采 · 站点合并 · CSV / Excel / GeoJSON")
        subtitle.setObjectName("SubTitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        title_row.addLayout(title_box)
        title_row.addStretch()
        self.api_status = QLabel("API 未测试")
        title_row.addWidget(self.api_status)
        outer.addLayout(title_row)

        splitter = QSplitter(Qt.Horizontal)
        outer.addWidget(splitter, 1)

        left = self._card()
        left.setMinimumWidth(370)
        left.setMaximumWidth(450)
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(18, 18, 18, 18)
        left_layout.setSpacing(12)

        api_title = QLabel("1. API 与采集范围")
        api_title.setStyleSheet("font-size:16px;font-weight:700;")
        left_layout.addWidget(api_title)

        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.Password)
        self.key_edit.setPlaceholderText("输入高德 Web 服务 API Key")
        self.remember_key = QCheckBox("使用系统凭据库记住 Key")
        self.test_btn = QPushButton("测试 Key")
        self.test_btn.clicked.connect(self.test_key)

        form = QFormLayout()
        form.setSpacing(10)
        form.addRow("Web Key", self.key_edit)
        form.addRow("", self.remember_key)
        form.addRow("", self.test_btn)

        self.scope_mode = QComboBox()
        self.scope_mode.addItem("单城市", "city")
        self.scope_mode.addItem("省域（自动遍历地级市）", "province")
        self.scope_mode.currentIndexChanged.connect(self._scope_mode_changed)
        form.addRow("范围级别", self.scope_mode)

        search_row = QHBoxLayout()
        self.scope_search = QLineEdit()
        self.scope_search.setPlaceholderText("例如：榆林 / 陕西")
        self.search_btn = QPushButton("搜索")
        self.search_btn.clicked.connect(self.search_scope)
        search_row.addWidget(self.scope_search, 1)
        search_row.addWidget(self.search_btn)
        form.addRow("行政区", search_row)

        self.scope_result = QComboBox()
        self.scope_result.setPlaceholderText("请先搜索")
        self.scope_result.currentIndexChanged.connect(self.select_scope_result)
        form.addRow("搜索结果", self.scope_result)

        self.selected_label = QLabel("尚未选择")
        self.selected_label.setWordWrap(True)
        self.selected_label.setStyleSheet("padding:8px;background:#f8fafc;border-radius:8px;color:#475569;")
        form.addRow("已选范围", self.selected_label)

        left_layout.addLayout(form)

        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        left_layout.addWidget(line)

        opt_title = QLabel("2. 采集参数")
        opt_title.setStyleSheet("font-size:16px;font-weight:700;")
        left_layout.addWidget(opt_title)

        form2 = QFormLayout()
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.15, 5.0)
        self.interval_spin.setSingleStep(0.05)
        self.interval_spin.setValue(0.35)
        self.interval_spin.setSuffix(" 秒/请求")
        form2.addRow("请求间隔", self.interval_spin)

        self.merge_radius = QSpinBox()
        self.merge_radius.setRange(20, 500)
        self.merge_radius.setValue(120)
        self.merge_radius.setSuffix(" m")
        form2.addRow("站点合并半径", self.merge_radius)

        out_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("选择输出目录")
        self.output_btn = QPushButton("浏览")
        self.output_btn.clicked.connect(self.choose_output)
        out_row.addWidget(self.output_edit, 1)
        out_row.addWidget(self.output_btn)
        form2.addRow("输出目录", out_row)
        left_layout.addLayout(form2)

        note = QLabel("说明：官方接口没有“列出某城市全部公交线路”的单一接口。本程序采用“公交站 POI 空间发现 → 站点公交线路 ID → 线路 ID 详情”的链路，并对高密度区域自动细分网格。")
        note.setWordWrap(True)
        note.setStyleSheet("color:#64748b;background:#f8fafc;padding:10px;border-radius:8px;")
        left_layout.addWidget(note)

        left_layout.addStretch()

        action_row = QHBoxLayout()
        self.start_btn = QPushButton("开始 / 继续采集")
        self.start_btn.setObjectName("Primary")
        self.start_btn.clicked.connect(self.start_crawl)
        self.stop_btn = QPushButton("停止")
        self.stop_btn.setObjectName("Danger")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self.stop_crawl)
        action_row.addWidget(self.start_btn, 1)
        action_row.addWidget(self.stop_btn)
        left_layout.addLayout(action_row)

        self.export_btn = QPushButton("导出当前数据库")
        self.export_btn.setEnabled(False)
        self.export_btn.clicked.connect(self.export_results)
        left_layout.addWidget(self.export_btn)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

        metrics = QGridLayout()
        self.metric_labels = {}
        for col, (key, label) in enumerate([
            ("api_calls", "本次 API 调用"),
            ("pois", "发现 POI"),
            ("raw_stops", "原始公交站"),
            ("lines", "公交线路"),
            ("stations", "合并站点"),
        ]):
            card = self._card()
            lay = QVBoxLayout(card)
            num = QLabel("0")
            num.setObjectName("MetricNumber")
            cap = QLabel(label)
            cap.setObjectName("MetricLabel")
            lay.addWidget(num)
            lay.addWidget(cap)
            metrics.addWidget(card, 0, col)
            self.metric_labels[key] = num
        right_layout.addLayout(metrics)

        progress_card = self._card()
        pc = QVBoxLayout(progress_card)
        self.stage_label = QLabel("等待开始")
        self.stage_label.setStyleSheet("font-weight:600;")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        pc.addWidget(self.stage_label)
        pc.addWidget(self.progress_bar)
        right_layout.addWidget(progress_card)

        self.tabs = QTabWidget()
        self.lines_table = QTableWidget(0, 7)
        self.lines_table.setHorizontalHeaderLabels(["线路", "起点", "终点", "首班", "末班", "公司", "线路ID"])
        self.lines_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.lines_table.horizontalHeader().setStretchLastSection(True)
        self.lines_table.setAlternatingRowColors(True)

        self.stops_table = QTableWidget(0, 6)
        self.stops_table.setHorizontalHeaderLabels(["站点", "经度", "纬度", "线路数", "经过线路", "站点ID"])
        self.stops_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.stops_table.horizontalHeader().setStretchLastSection(True)
        self.stops_table.setAlternatingRowColors(True)

        self.preview = GeometryPreview()
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)

        self.tabs.addTab(self.lines_table, "线路")
        self.tabs.addTab(self.stops_table, "合并站点")
        self.tabs.addTab(self.preview, "空间预览")
        self.tabs.addTab(self.log_text, "运行日志")
        right_layout.addWidget(self.tabs, 1)

        splitter.addWidget(left)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)

    def _restore_settings(self):
        out = self.settings.value("output_dir", str(Path.home() / "TransitCollectorData"))
        self.output_edit.setText(out)
        self.interval_spin.setValue(float(self.settings.value("interval", 0.35)))
        self.merge_radius.setValue(int(self.settings.value("merge_radius", 120)))
        if self.settings.value("remember_key", False, type=bool):
            try:
                key = keyring.get_password(KEYRING_SERVICE, "amap_web_key") or ""
                self.key_edit.setText(key)
                self.remember_key.setChecked(bool(key))
            except Exception:
                pass

    def _save_settings(self):
        self.settings.setValue("output_dir", self.output_edit.text().strip())
        self.settings.setValue("interval", self.interval_spin.value())
        self.settings.setValue("merge_radius", self.merge_radius.value())
        self.settings.setValue("remember_key", self.remember_key.isChecked())
        try:
            if self.remember_key.isChecked() and self.key_edit.text().strip():
                keyring.set_password(KEYRING_SERVICE, "amap_web_key", self.key_edit.text().strip())
            elif not self.remember_key.isChecked():
                keyring.delete_password(KEYRING_SERVICE, "amap_web_key")
        except keyring.errors.PasswordDeleteError:
            pass
        except Exception:
            pass

    def closeEvent(self, event):
        self._save_settings()
        if self.crawl_thread and self.crawl_thread.isRunning():
            self.crawl_thread.stop()
            self.crawl_thread.wait(3000)
        super().closeEvent(event)

    def _scope_mode_changed(self):
        self.scope_result.clear()
        self.search_results = []
        self.selected_scope = None
        self.selected_label.setText("尚未选择")
        mode = self.scope_mode.currentData()
        self.scope_search.setPlaceholderText("例如：榆林" if mode == "city" else "例如：陕西")

    def test_key(self):
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 Key", "请先输入高德 Web 服务 API Key。")
            return
        try:
            client = AMapClient(key, min_interval=self.interval_spin.value())
            client.test_key()
            self.api_status.setText("API 可用")
            self.api_status.setStyleSheet("color:#15803d;font-weight:600;")
            self.append_log("API Key 测试通过。")
            self._save_settings()
        except Exception as e:
            self.api_status.setText("API 测试失败")
            self.api_status.setStyleSheet("color:#b91c1c;font-weight:600;")
            QMessageBox.critical(self, "API 测试失败", str(e))

    def search_scope(self):
        key = self.key_edit.text().strip()
        keyword = self.scope_search.text().strip()
        if not key or not keyword:
            QMessageBox.warning(self, "参数不完整", "请填写 Key 和行政区搜索关键字。")
            return
        self.search_btn.setEnabled(False)
        self.scope_result.clear()
        mode = self.scope_mode.currentData()
        self.search_thread = DistrictSearchThread(key, keyword, mode, self.interval_spin.value())
        self.search_thread.done.connect(self._search_done)
        self.search_thread.failed.connect(self._search_failed)
        self.search_thread.start()

    def _search_done(self, items):
        self.search_btn.setEnabled(True)
        self.search_results = items
        self.scope_result.blockSignals(True)
        self.scope_result.clear()
        for d in items:
            self.scope_result.addItem(f"{d.name} · {d.level} · {d.adcode}", d.adcode)
        self.scope_result.blockSignals(False)
        if items:
            self.scope_result.setCurrentIndex(0)
            self.select_scope_result(0)
        else:
            QMessageBox.information(self, "无匹配", "没有找到符合当前范围级别的行政区。")

    def _search_failed(self, msg):
        self.search_btn.setEnabled(True)
        QMessageBox.critical(self, "行政区搜索失败", msg)

    def select_scope_result(self, idx):
        if idx < 0 or idx >= len(self.search_results):
            return
        d = self.search_results[idx]
        mode = self.scope_mode.currentData()
        level = "province" if mode == "province" else "city"
        self.selected_scope = Scope(d.name, d.adcode, level, d.citycode)
        self.selected_label.setText(f"{d.name}\nADCODE：{d.adcode}\n模式：{'省域' if level=='province' else '单城市'}")

    def choose_output(self):
        path = QFileDialog.getExistingDirectory(self, "选择输出目录", self.output_edit.text().strip() or str(Path.home()))
        if path:
            self.output_edit.setText(path)

    def _db_path_for_scope(self):
        out = Path(self.output_edit.text().strip())
        out.mkdir(parents=True, exist_ok=True)
        safe = self.selected_scope.adcode if self.selected_scope else "unknown"
        return out / f"transit_{safe}.sqlite"

    def start_crawl(self):
        if self.crawl_thread and self.crawl_thread.isRunning():
            return
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 Key", "请先输入高德 Web 服务 API Key。")
            return
        if not self.selected_scope:
            QMessageBox.warning(self, "未选择范围", "请先搜索并选择城市或省域。")
            return
        if not self.output_edit.text().strip():
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return

        self._save_settings()
        db_path = self._db_path_for_scope()
        self.last_db_path = db_path

        self.log_text.clear()
        self.append_log(f"数据库：{db_path}")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self.progress_bar.setValue(0)

        self.crawl_thread = CrawlThread(
            key,
            self.selected_scope,
            db_path,
            self.interval_spin.value(),
            float(self.merge_radius.value()),
        )
        self.crawl_thread.log_sig.connect(self.append_log)
        self.crawl_thread.progress_sig.connect(self.update_progress)
        self.crawl_thread.metrics_sig.connect(self.update_metrics)
        self.crawl_thread.done_sig.connect(self.crawl_done)
        self.crawl_thread.failed_sig.connect(self.crawl_failed)
        self.crawl_thread.start()

    def stop_crawl(self):
        if self.crawl_thread and self.crawl_thread.isRunning():
            self.append_log("正在请求停止…")
            self.crawl_thread.stop()
            self.stop_btn.setEnabled(False)

    def update_progress(self, stage, current, total):
        self.stage_label.setText(stage)
        pct = int(current / max(total, 1) * 100)
        self.progress_bar.setValue(max(0, min(100, pct)))

    def update_metrics(self, metrics):
        for k, label in self.metric_labels.items():
            label.setText(str(metrics.get(k, 0)))

    def append_log(self, text):
        self.log_text.append(str(text))

    def crawl_done(self, db_path):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.export_btn.setEnabled(True)
        self.stage_label.setText("采集完成")
        self.progress_bar.setValue(100)
        self.last_db_path = Path(db_path)
        self.refresh_preview()
        QMessageBox.information(self, "完成", "采集完成。点击“导出当前数据库”生成 CSV、Excel 和 GeoJSON。")

    def crawl_failed(self, msg):
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.export_btn.setEnabled(bool(self.last_db_path and self.last_db_path.exists()))
        self.stage_label.setText("已停止 / 未完成")
        self.append_log(msg)
        QMessageBox.warning(self, "采集未完成", msg)

    def refresh_preview(self):
        if not self.last_db_path or not self.last_db_path.exists():
            return
        db = TransitDB(self.last_db_path)
        try:
            lines = db.conn.execute("""
                SELECT line_id,name,start_stop,end_stop,start_time,end_time,company
                FROM bus_lines ORDER BY name LIMIT 3000
            """).fetchall()
            self.lines_table.setRowCount(len(lines))
            for r, row in enumerate(lines):
                vals = [row["name"], row["start_stop"], row["end_stop"], row["start_time"], row["end_time"], row["company"], row["line_id"]]
                for c, v in enumerate(vals):
                    self.lines_table.setItem(r, c, QTableWidgetItem(str(v or "")))

            stops = db.conn.execute("""
                SELECT sg.station_id,sg.name,sg.longitude,sg.latitude,sg.line_count,
                       GROUP_CONCAT(DISTINCT bl.name) AS line_names
                FROM station_groups sg
                LEFT JOIN station_lines sl ON sl.station_id=sg.station_id
                LEFT JOIN bus_lines bl ON bl.line_id=sl.line_id
                GROUP BY sg.station_id ORDER BY sg.name LIMIT 5000
            """).fetchall()
            self.stops_table.setRowCount(len(stops))
            for r, row in enumerate(stops):
                vals = [
                    row["name"],
                    "" if row["longitude"] is None else f"{row['longitude']:.6f}",
                    "" if row["latitude"] is None else f"{row['latitude']:.6f}",
                    row["line_count"],
                    row["line_names"] or "",
                    row["station_id"],
                ]
                for c, v in enumerate(vals):
                    self.stops_table.setItem(r, c, QTableWidgetItem(str(v)))
            self.preview.draw_points(stops)
        finally:
            db.close()

    def export_results(self):
        path = self.last_db_path or self._db_path_for_scope()
        if not path.exists():
            QMessageBox.warning(self, "无数据库", "尚未找到可导出的采集数据库。")
            return
        export_dir = path.parent / (path.stem + "_export")
        db = TransitDB(path)
        try:
            db.rebuild_station_groups(float(self.merge_radius.value()))
            files = Exporter(db).export_all(export_dir)
        finally:
            db.close()
        self.refresh_preview()
        QMessageBox.information(self, "导出完成", f"已导出到：\n{export_dir}")
        self.append_log("导出文件：\n" + "\n".join(files.values()))


def run_app():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = MainWindow()
    win.show()
    sys.exit(app.exec())
