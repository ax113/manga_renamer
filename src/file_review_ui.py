"""Plain file review rows and discoverable, read-only text copying."""
from PySide6.QtCore import Qt, QPersistentModelIndex, QEvent
from PySide6.QtGui import QColor, QPalette, QKeySequence, QPen
from PySide6.QtWidgets import (QApplication, QDialog, QVBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QMenu, QStyle, QStyleOptionButton,
    QStyleOptionViewItem, QTreeWidget, QAbstractItemView, QHeaderView)
from .review_ui import ComicTable, ComicDelegate

from .review_ui import (ReviewTree, GROUP_START_ROLE, SELECTED_COLOR, HOVER_COLOR)
CONFLICT_COLORS = ('#fff0df', '#eee7f6')
UNDO_COLOR = '#f3effa'


class FileRowDelegate(ComicDelegate):
    pass


class FileTable(ComicTable):
    def __init__(self, labels, widths, check_column=None):
        super().__init__()
        self.check_column = check_column
        self._file_widths = widths
        self.setColumnCount(len(labels))
        self.setHorizontalHeaderLabels(labels)
        self._hover_index = None
        self.setItemDelegate(FileRowDelegate(self))
        self.setEditTriggers(ComicTable.NoEditTriggers)
        self.setSelectionMode(ComicTable.ExtendedSelection)
        self.setSelectionBehavior(ComicTable.SelectRows)
        self.verticalHeader().hide()
        self.setAlternatingRowColors(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setWordWrap(False)
        self.setShowGrid(False)
        self.setToolTip('右键复制完整内容，或选中后按 Ctrl+C；双击查看并复制部分文字。')
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)

    def showEvent(self, event):
        first = not self._initial_widths
        super().showEvent(event)
        if first:
            for column, width in enumerate(self._file_widths):
                self.setColumnWidth(column, width)

    def mouseMoveEvent(self, event):
        self._hover_index = QPersistentModelIndex(self.indexAt(event.position().toPoint()))
        self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_index = None
        self.viewport().update()
        super().leaveEvent(event)

    def copy_cell(self, row, column):
        item = self.item(row, column)
        if item is not None and column != self.check_column:
            QApplication.clipboard().setText(item.text())

    def copy_rows(self, rows):
        lines = []
        for row in sorted(set(rows)):
            lines.append('\t'.join(self.item(row, c).text() if self.item(row, c) else ''
                for c in range(self.columnCount()) if c != self.check_column))
        QApplication.clipboard().setText('\n'.join(lines))

    def copy_menu(self, row, column):
        menu = QMenu(self)
        label = self.horizontalHeaderItem(column).text()
        title = '复制' + label if label in ('原路径', '目标路径') else '复制此单元格内容'
        menu.addAction(title, lambda: self.copy_cell(row, column))
        for c in range(self.columnCount()):
            name = self.horizontalHeaderItem(c).text()
            if name in ('原路径', '目标路径') and c != column:
                menu.addAction('复制' + name, lambda c=c: self.copy_cell(row, c))
        menu.addSeparator()
        menu.addAction('复制整行信息', lambda: self.copy_rows([row]))
        menu.addAction('查看完整文字', lambda: self.view_text(row, column))
        return menu

    def _context_menu(self, point):
        index = self.indexAt(point)
        if index.isValid() and index.column() != self.check_column:
            self.copy_menu(index.row(), index.column()).exec(self.viewport().mapToGlobal(point))

    def text_dialog(self, row, column):
        dialog = QDialog(self)
        dialog.setWindowTitle('完整文字 · ' + self.horizontalHeaderItem(column).text())
        dialog.resize(760, 360)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel('可拖选部分文字复制，也可按 Ctrl+A、Ctrl+C 复制全部。'))
        text = QPlainTextEdit()
        text.setObjectName('full_cell_text')
        text.setReadOnly(True)
        text.setPlainText(self.item(row, column).text())
        layout.addWidget(text)
        close = QPushButton('关闭')
        close.clicked.connect(dialog.accept)
        layout.addWidget(close)
        return dialog

    def view_text(self, row, column):
        if self.item(row, column) is not None and column != self.check_column:
            self.text_dialog(row, column).exec()

    def mouseDoubleClickEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        if index.isValid() and index.column() != self.check_column:
            self._check_pressed = None
            self.view_text(index.row(), index.column())
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            rows = {i.row() for i in self.selectedIndexes()}
            if len(rows) > 1:
                self.copy_rows(rows)
            elif self.currentRow() >= 0:
                self.copy_cell(self.currentRow(), self.currentColumn())
            event.accept()
        else:
            super().keyPressEvent(event)


class FileHistory(ReviewTree):
    """Keep receipt columns while placing undo receipts beneath their rename."""
    check_column = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_index = None
        self.setHeaderLabels(['时间', '漫画库', '类型', '总数', '成功', '失败', '未执行', '状态', '撤销情况'])
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setItemDelegate(FileRowDelegate(self))
        self.setAlternatingRowColors(False)
        self.setMouseTracking(True)
        self.setUniformRowHeights(True)
        self.setAllColumnsShowFocus(False)
        palette = self.palette()
        palette.setColor(QPalette.Highlight, QColor(SELECTED_COLOR))
        palette.setColor(QPalette.HighlightedText, palette.color(QPalette.Text))
        self.setPalette(palette)
        self.header().setSectionResizeMode(QHeaderView.Interactive)
        self.header().setStretchLastSection(True)
        for i, width in enumerate((180, 90, 130, 65, 65, 65, 75, 130, 140)):
            self.setColumnWidth(i, width)
        from .review_ui import ReservedScrollBar
        self.setHorizontalScrollBar(ReservedScrollBar(Qt.Horizontal, self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.compact_time_column()
        self.setToolTip('展开改名或移动任务可查看对应撤销记录；选中父行或子行查看各自明细。')

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and self.columnCount() >= 9:
            self.compact_time_column()

    def compact_time_column(self):
        # Child timestamp starts immediately after the parent's first two digits (20).
        indent = max(10, self.fontMetrics().horizontalAdvance('20'))
        self.setIndentation(indent)
        self.setColumnWidth(0, self.fontMetrics().horizontalAdvance('2026-10-07 00:00:00') + 2*indent + 16)



def undo_summary(task, children):
    eligible = {r['local_id'] for r in task['rows'] if r['state'] == 'success'}
    undone = set()
    for child in children:
        if child['library_id'] != task['library_id']: continue
        for row in child['rows']:
            for entry in row.get('steps') or [row]:
                if entry['state']=='success' and entry.get('reverses_task_id',child.get('parent_task_id')) == task['task_id']:
                    undone.add(entry.get('reverses_row',row.get('parent_row',row['local_id'])))
    undone &= eligible
    remaining = len(eligible - undone)
    label = ('全部已撤销' if eligible and not remaining else
             '部分已撤销' if undone else '未撤销')
    return label, remaining
