from __future__ import annotations

import csv
import math
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class _OperationTableSelectionDelegate(QStyledItemDelegate):
    """Paint selected, hovered, and focused cells without native dark highlights."""

    def paint(self, painter, option, index):
        active = QStyle.State_Selected | QStyle.State_MouseOver | QStyle.State_HasFocus
        if not option.state & active:
            super().paint(painter, option, index)
            return

        painter.save()
        painter.fillRect(option.rect, QColor(242, 238, 250))  # 漫画勾选卡片内部填充色
        painter.setPen(QColor(36, 27, 53))
        painter.setFont(option.font)
        text_rect = option.rect.adjusted(4, 0, -4, 0)
        text = str(index.data(Qt.DisplayRole) or "")
        painter.drawText(
            text_rect,
            Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine,
            option.fontMetrics.elidedText(text, Qt.ElideRight, max(1, text_rect.width())),
        )
        painter.restore()


class OperationPreviewDialog(QDialog):
    """大批量模拟/冲突结果窗口。

    不直接执行任何硬盘操作，只负责分页展示、筛选和导出。
    """

    def __init__(self, rows: list[dict], title: str = "模拟操作清单", parent=None):
        super().__init__(parent)
        self.rows = list(rows)
        self.filtered_rows: list[dict] = []
        self.page = 0
        self.setWindowTitle(title)
        self.resize(1180, 760)

        root = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        root.addWidget(self.summary)

        tools = QHBoxLayout()
        tools.addWidget(QLabel("显示："))
        self.filter_combo = QComboBox()
        self.filter_combo.addItem("全部", "all")
        self.filter_combo.addItem("只看有变化", "changed")
        self.filter_combo.addItem("只看冲突", "conflict")
        self.filter_combo.currentIndexChanged.connect(self._apply_filter)
        tools.addWidget(self.filter_combo)
        tools.addSpacing(16)
        tools.addWidget(QLabel("每页："))
        self.page_size_combo = QComboBox()
        self.page_size_combo.addItems(["100", "500"])
        self.page_size_combo.currentIndexChanged.connect(self._page_size_changed)
        tools.addWidget(self.page_size_combo)
        tools.addStretch(1)
        self.export_btn = QPushButton("导出完整清单")
        self.export_btn.clicked.connect(self._export)
        tools.addWidget(self.export_btn)
        root.addLayout(tools)

        self.table = QTableWidget(0, 5)
        self.table.setItemDelegate(_OperationTableSelectionDelegate(self.table))
        self.table.setMouseTracking(True)
        self.table.setHorizontalHeaderLabels(["状态", "当前名称", "最终名称", "目标位置", "说明"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        root.addWidget(self.table, 1)

        nav = QHBoxLayout()
        self.prev_btn = QPushButton("上一页")
        self.next_btn = QPushButton("下一页")
        self.page_label = QLabel()
        self.prev_btn.clicked.connect(self._prev_page)
        self.next_btn.clicked.connect(self._next_page)
        nav.addStretch(1)
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.page_label)
        nav.addWidget(self.next_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        nav.addStretch(1)
        nav.addWidget(close_btn)
        root.addLayout(nav)

        self._apply_filter()

    def _page_size(self) -> int:
        try:
            return int(self.page_size_combo.currentText())
        except Exception:
            return 100

    @staticmethod
    def _is_conflict(row: dict) -> bool:
        return bool(row.get("blocking_conflict"))

    @staticmethod
    def _is_changed(row: dict) -> bool:
        return bool(row.get("changed"))

    def _apply_filter(self, *_args):
        mode = self.filter_combo.currentData() or "all"
        if mode == "changed":
            self.filtered_rows = [r for r in self.rows if self._is_changed(r)]
        elif mode == "conflict":
            self.filtered_rows = [r for r in self.rows if self._is_conflict(r)]
        else:
            self.filtered_rows = list(self.rows)
        self.page = 0
        self._refresh()

    def _page_size_changed(self, *_args):
        self.page = 0
        self._refresh()

    def _page_count(self) -> int:
        size = self._page_size()
        return max(1, math.ceil(len(self.filtered_rows) / max(1, size)))

    def _refresh(self):
        total = len(self.rows)
        changed = sum(1 for r in self.rows if self._is_changed(r))
        conflicts = sum(1 for r in self.rows if self._is_conflict(r))
        occupancy = sum(1 for r in self.rows if r.get("conflict_kind") == "temporary_occupancy")
        self.summary.setText(
            f"共 {total} 项｜名称/位置有变化 {changed}｜阻断冲突 {conflicts}"
            + (f"｜可自动处理的临时占位 {occupancy}" if occupancy else "")
        )

        page_count = self._page_count()
        self.page = min(max(0, self.page), page_count - 1)
        size = self._page_size()
        start = self.page * size
        page_rows = self.filtered_rows[start:start + size]
        self.table.setRowCount(len(page_rows))
        for r, row in enumerate(page_rows):
            values = [
                str(row.get("status", "")),
                str(row.get("current_name", "")),
                str(row.get("final_name", "")),
                str(row.get("target_path", "")),
                str(row.get("message", "")),
            ]
            for c, value in enumerate(values):
                cell = QTableWidgetItem(value)
                cell.setToolTip(value)
                self.table.setItem(r, c, cell)
        self.page_label.setText(f"第 {self.page + 1} / {page_count} 页　当前筛选 {len(self.filtered_rows)} 项")
        self.prev_btn.setEnabled(self.page > 0)
        self.next_btn.setEnabled(self.page + 1 < page_count)

    def _prev_page(self):
        if self.page > 0:
            self.page -= 1
            self._refresh()

    def _next_page(self):
        if self.page + 1 < self._page_count():
            self.page += 1
            self._refresh()

    def _export(self):
        path, _ = QFileDialog.getSaveFileName(
            self,
            "导出完整清单",
            "模拟操作清单.tsv",
            "TSV 文本 (*.tsv);;所有文件 (*)",
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                writer = csv.writer(f, delimiter="\t")
                writer.writerow(["状态", "当前名称", "最终名称", "目标位置", "说明", "local_id"])
                for row in self.rows:
                    writer.writerow([
                        row.get("status", ""), row.get("current_name", ""), row.get("final_name", ""),
                        row.get("target_path", ""), row.get("message", ""), row.get("local_id", ""),
                    ])
            QMessageBox.information(self, "导出完成", f"已导出 {len(self.rows)} 项。")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", str(exc))
