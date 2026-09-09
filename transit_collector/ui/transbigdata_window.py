from __future__ import annotations

import hashlib
import re
import sys
import threading
import traceback
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
)

from transit_collector.crawler import Scope
from transit_collector.db import TransitDB
from transit_collector.transbigdata_backend import DiscoveryConfig, TransBigDataCollector
from transit_collector.ui.integrated_window import IntegratedMainWindow
from transit_collector.ui.main_window import APP_NAME


class TransBigDataCrawlThread(QThread):
    log_sig = Signal(str)
    progress_sig = Signal(str, int, int)
    metrics_sig = Signal(dict)
    done_sig = Signal(str)
    failed_sig = Signal(str)

    def __init__(
        self,
        city: str,
        db_path: Path,
        config: DiscoveryConfig,
        merge_radius: float,
    ):
        super().__init__()
        self.city = city
        self.db_path = db_path
        self.config = config
        self.merge_radius = merge_radius
        self.cancel_event = threading.Event()

    def stop(self):
        self.cancel_event.set()

    def run(self):
        db = None
        try:
            db = TransitDB(self.db_path)
            collector = TransBigDataCollector(
                city=self.city,
                db=db,
                config=self.config,
                merge_radius_m=self.merge_radius,
                cancel_event=self.cancel_event,
                log=self.log_sig.emit,
                progress=self.progress_sig.emit,
                metrics=self.metrics_sig.emit,
            )
            collector.run()
            self.done_sig.emit(str(self.db_path))
        except InterruptedError:
            self.failed_sig.emit("采集已停止，线路扫描和已获取线路均已落盘；再次开始会从断点继续。")
        except Exception:
            self.failed_sig.emit(traceback.format_exc())
        finally:
            if db:
                db.close()


class TransitWorkbenchWindow(IntegratedMainWindow):
    """Integrated AMap + TransBigData/Baidu desktop workbench.

    TransBigData is the default source. It does not need an AMap key for bus
    routes. The original AMap Web Service collector remains available as a
    second source for users with a correct Web Service key.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("公交线路与站点 GIS 采集分析器 · TransBigData / 高德")
        self._source_note_label = None
        for label in self.findChildren(QLabel):
            if label.text() == "公交线路与站点采集器":
                label.setText("公交线路与站点 GIS 采集分析器")
            elif label.text() == "发现 POI":
                label.setText("发现线路词")
            elif label.text().startswith("说明：官方接口没有"):
                self._source_note_label = label

        self._install_source_bar()
        saved = str(self.settings.value("data_source", "transbigdata"))
        idx = self.source_combo.findData(saved)
        self.source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        scan = str(self.settings.value("tbd_scan_mode", "full"))
        scan_idx = self.scan_combo.findData(scan)
        self.scan_combo.setCurrentIndex(scan_idx if scan_idx >= 0 else 0)
        self.extra_keywords_edit.setText(str(self.settings.value("tbd_extra_keywords", "")))
        self._source_changed()

    def _install_source_bar(self):
        card = QFrame()
        card.setObjectName("Card")
        row = QHBoxLayout(card)
        row.setContentsMargins(14, 10, 14, 10)

        row.addWidget(QLabel("数据源"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("TransBigData / 百度线路（推荐）", "transbigdata")
        self.source_combo.addItem("高德 Web 服务（原模式）", "amap")
        self.source_combo.setMinimumWidth(240)
        self.source_combo.currentIndexChanged.connect(self._source_changed)
        row.addWidget(self.source_combo)

        row.addWidget(QLabel("线路扫描"))
        self.scan_combo = QComboBox()
        self.scan_combo.addItem("完整：1-999 + 常见前缀", "full")
        self.scan_combo.addItem("快速：1-300 + 常见前缀", "quick")
        self.scan_combo.setMinimumWidth(210)
        row.addWidget(self.scan_combo)

        row.addWidget(QLabel("额外线路关键词"))
        self.extra_keywords_edit = QLineEdit()
        self.extra_keywords_edit.setPlaceholderText("例如：榆横城际公交,机场专线；多个名称用逗号分隔")
        self.extra_keywords_edit.setToolTip(
            "用于补充纯名称线路。程序已自动扫描数字线路、K/B/Y/夜/游/快等前缀，并分页搜索城际/专线/机场等关键词。"
        )
        row.addWidget(self.extra_keywords_edit, 1)

        outer = self.centralWidget().layout()
        outer.insertWidget(1, card)

    def _source_is_tbd(self) -> bool:
        return getattr(self, "source_combo", None) is not None and self.source_combo.currentData() == "transbigdata"

    def _source_changed(self):
        if not hasattr(self, "source_combo"):
            return
        is_tbd = self._source_is_tbd()
        self.scan_combo.setEnabled(is_tbd)
        self.extra_keywords_edit.setEnabled(is_tbd)
        if is_tbd:
            self.scope_mode.setCurrentIndex(0)
            self.scope_mode.setEnabled(False)
            self.key_edit.setEnabled(False)
            self.remember_key.setEnabled(False)
            self.test_btn.setEnabled(False)
            self.key_edit.setPlaceholderText("TransBigData 模式无需高德 Key")
            self.scope_search.setPlaceholderText("直接输入城市，例如：榆林市 / 汉中市")
            self.search_btn.setText("使用城市")
            self.scope_result.setEnabled(True)
            self.api_status.setText("TransBigData / 百度：无需 Key")
            self.api_status.setStyleSheet("color:#15803d;font-weight:600;")
            if self._source_note_label:
                self._source_note_label.setText(
                    "说明：TransBigData 的 getbusdata 本身需要线路关键词。旧程序依赖 8684.cn 获取全市线路名；"
                    "新版已移除该依赖，先用与 TransBigData 相同的百度地图搜索机制分页发现命名线路，并扫描"
                    "数字线路及 K/B/Y/夜/游/快等常见前缀，再把去重后的线路关键词交给 TransBigData 获取"
                    "线路几何和完整站序。扫描进度写入 SQLite，可断点继续。"
                )
        else:
            self.scope_mode.setEnabled(True)
            self.key_edit.setEnabled(True)
            self.remember_key.setEnabled(True)
            self.test_btn.setEnabled(True)
            self.key_edit.setPlaceholderText("输入高德 Web 服务 API Key")
            self.scope_search.setPlaceholderText("例如：榆林 / 陕西")
            self.search_btn.setText("搜索")
            self.api_status.setText("高德 Web 服务：API 未测试")
            self.api_status.setStyleSheet("")
            if self._source_note_label:
                self._source_note_label.setText(
                    "说明：高德模式采用行政区边界 → 公交站 POI 空间发现 → 站点公交线路 ID → 线路 ID 详情，"
                    "需要“服务平台=Web服务”的高德 Key。"
                )

    def _save_settings(self):
        super()._save_settings()
        if hasattr(self, "source_combo"):
            self.settings.setValue("data_source", self.source_combo.currentData())
            self.settings.setValue("tbd_scan_mode", self.scan_combo.currentData())
            self.settings.setValue("tbd_extra_keywords", self.extra_keywords_edit.text().strip())

    def test_key(self):
        if self._source_is_tbd():
            self.api_status.setText("TransBigData / 百度：无需 Key")
            return
        return super().test_key()

    def search_scope(self):
        if not self._source_is_tbd():
            return super().search_scope()
        city = self.scope_search.text().strip()
        if not city:
            QMessageBox.warning(self, "缺少城市", "请输入城市名称，例如“榆林市”或“汉中市”。")
            return
        token = hashlib.sha1(city.encode("utf-8")).hexdigest()[:10]
        self.selected_scope = Scope(city, f"TBD_{token}", "city", "")
        self.search_results = []
        self.scope_result.blockSignals(True)
        self.scope_result.clear()
        self.scope_result.addItem(f"{city} · TransBigData/百度", self.selected_scope.adcode)
        self.scope_result.setCurrentIndex(0)
        self.scope_result.blockSignals(False)
        self.selected_label.setText(f"{city}\n数据源：TransBigData / 百度\n模式：单城市")
        self.append_log(f"已选择 TransBigData 城市：{city}")

    def select_scope_result(self, idx):
        if self._source_is_tbd():
            return
        return super().select_scope_result(idx)

    def _db_path_for_scope(self):
        if not self._source_is_tbd():
            return super()._db_path_for_scope()
        out = Path(self.output_edit.text().strip())
        out.mkdir(parents=True, exist_ok=True)
        city = self.scope_search.text().strip() or (self.selected_scope.name if self.selected_scope else "city")
        safe = re.sub(r"[\\/:*?\"<>|\s]+", "_", city).strip("_") or "city"
        return out / f"transit_tbd_{safe}.sqlite"

    @staticmethod
    def _parse_extra_keywords(text: str) -> tuple[str, ...]:
        parts = re.split(r"[,，;；\n]+", text or "")
        return tuple(dict.fromkeys(p.strip() for p in parts if p.strip()))

    def start_crawl(self):
        if not self._source_is_tbd():
            return super().start_crawl()
        if self.crawl_thread and self.crawl_thread.isRunning():
            return
        city = self.scope_search.text().strip()
        if not city:
            QMessageBox.warning(self, "缺少城市", "请直接输入城市名称，然后开始采集。")
            return
        if not self.selected_scope or self.selected_scope.name != city:
            self.search_scope()
        if not self.output_edit.text().strip():
            QMessageBox.warning(self, "缺少输出目录", "请选择输出目录。")
            return

        self._save_settings()
        db_path = self._db_path_for_scope()
        self.last_db_path = db_path
        self.log_text.clear()
        self.append_log(f"数据源：TransBigData / 百度地图")
        self.append_log(f"城市：{city}")
        self.append_log(f"数据库：{db_path}")
        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.export_btn.setEnabled(False)
        self.progress_bar.setValue(0)

        extras = self._parse_extra_keywords(self.extra_keywords_edit.text())
        config = DiscoveryConfig.quick(extras) if self.scan_combo.currentData() == "quick" else DiscoveryConfig.full(extras)
        interval = float(self.interval_spin.value())
        config.delay_min = max(0.03, min(0.20, interval / 4.0))
        config.delay_max = max(config.delay_min, min(0.60, interval))

        self.crawl_thread = TransBigDataCrawlThread(
            city=city,
            db_path=db_path,
            config=config,
            merge_radius=float(self.merge_radius.value()),
        )
        self.crawl_thread.log_sig.connect(self.append_log)
        self.crawl_thread.progress_sig.connect(self.update_progress)
        self.crawl_thread.metrics_sig.connect(self.update_metrics)
        self.crawl_thread.done_sig.connect(self.crawl_done)
        self.crawl_thread.failed_sig.connect(self.crawl_failed)
        self.crawl_thread.start()


def run_app():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setStyle("Fusion")
    win = TransitWorkbenchWindow()
    win.show()
    sys.exit(app.exec())
