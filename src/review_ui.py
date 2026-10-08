"""Review controls follow the main window; reserve a native scrollbar gutter."""
from pathlib import Path
import os
from PySide6.QtCore import Qt, QEvent, QObject, QRect, QPoint, QPersistentModelIndex, QItemSelectionModel
from PySide6.QtGui import QPainter, QPalette, QColor, QPen, QBrush
from PySide6.QtWidgets import (QVBoxLayout, QScrollArea, QFrame, QSpinBox, QScrollBar,
    QComboBox, QLineEdit, QWidget, QStyledItemDelegate, QStyle, QStyleOptionViewItem,
    QStyleOptionButton, QStyleOptionHeader, QTableWidget, QHeaderView, QTreeWidget, QAbstractItemView, QStylePainter, QStyleOptionComboBox)

from .ui_common import AlignedComboBox


def page_layout(widget):
    white_page(widget)
    layout = QVBoxLayout(widget)
    layout.setContentsMargins(8, 8, 8, 8)
    layout.setSpacing(6)
    return layout


def white_page(widget):
    palette = widget.palette()
    palette.setColor(QPalette.Window, QColor('white'))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


def address_field():
    field = QLineEdit()
    field.setReadOnly(True)
    field.setMinimumWidth(0)
    return field


def set_address(field, value, placeholder='尚未设置'):
    field.setPlaceholderText(placeholder)
    if field.text() != value:
        field.setText(value)
        field.setCursorPosition(0)
    field.setToolTip(value or placeholder)


def match_tabs(tabs):
    # Apply only to tab bars; native inputs and disabled palettes remain intact.
    tabs.tabBar().setStyleSheet(
        'QTabBar::tab { background: #f0f0f0; border: 1px solid #d5d5d5; '
        'padding: 5px 10px; margin: 0px; } '
        'QTabBar::tab:selected { background: white; border-bottom-color: white; }')


def result_paths(mime):
    if not mime.hasUrls():
        return []
    paths, seen = [], set()
    for url in mime.urls():
        if not url.isLocalFile():
            continue
        path = url.toLocalFile()
        identity = os.path.normcase(os.path.abspath(path))
        if identity not in seen and Path(path).suffix.lower() == '.txt' and Path(path).is_file():
            paths.append(path)
            seen.add(identity)
    return paths


class ResultPageDrop(QObject):
    """Route file drops on the page and child viewports through one importer."""
    def __init__(self, root, owner):
        super().__init__(root)
        self.owner = owner
        for widget in [root, *root.findChildren(QWidget)]:
            widget.setAcceptDrops(True)
            widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if event.type() not in (QEvent.DragEnter, QEvent.DragMove, QEvent.Drop):
            return False
        if not event.mimeData().hasUrls():
            return False
        paths = result_paths(event.mimeData())
        if not paths:
            event.ignore()
            return True
        event.acceptProposedAction()
        if event.type() == QEvent.Drop:
            self.owner.import_txt_files(paths)
        return True


SELECTED_COLOR = '#e5f1ff'
HOVER_COLOR = '#eeeeee'
GRID_COLOR = '#d6d6d6'
GROUP_START_ROLE = Qt.UserRole + 51


def cell_background(view, index, selected=False):
    if selected:
        return QColor(SELECTED_COLOR)
    if getattr(view, '_hover_index', None) == index:
        return QColor(HOVER_COLOR)
    brush = index.data(Qt.BackgroundRole)
    return brush.color() if brush is not None and brush.style() != Qt.NoBrush else QColor('white')


class ScopeComboBox(AlignedComboBox):
    """Compatibility name for the two shared review scope selectors."""
    pass


class ComicDelegate(QStyledItemDelegate):
    """Common cell grid, cell hover and whole-row selection for review tables."""
    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        size.setHeight(max(30, option.fontMetrics.height()+10))
        return size

    def check_rect(self, option):
        style = option.widget.style()
        width = style.pixelMetric(QStyle.PM_IndicatorWidth, None, option.widget)
        height = style.pixelMetric(QStyle.PM_IndicatorHeight, None, option.widget)
        return QRect(option.rect.center().x()-width//2, option.rect.center().y()-height//2, width, height)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        painter.save()
        painter.fillRect(opt.rect, cell_background(opt.widget, index, bool(opt.state & QStyle.State_Selected)))
        if index.column() == getattr(opt.widget, 'check_column', 0):
            check = QStyleOptionButton()
            check.rect = self.check_rect(opt)
            check.state = QStyle.State_Enabled if index.flags() & Qt.ItemIsUserCheckable else QStyle.State_None
            check.state |= QStyle.State_On if Qt.CheckState(index.data(Qt.CheckStateRole)) == Qt.Checked else QStyle.State_Off
            opt.widget.style().drawPrimitive(QStyle.PE_IndicatorItemViewItemCheck, check, painter, opt.widget)
        else:
            painter.setFont(opt.font)
            painter.setPen(opt.palette.color(QPalette.Text))
            rect = opt.rect.adjusted(6, 0, -6, 0)
            alignment = index.data(Qt.TextAlignmentRole) or (Qt.AlignLeft | Qt.AlignVCenter)
            if opt.features & QStyleOptionViewItem.WrapText:
                painter.save()
                painter.setClipRect(rect)
                painter.drawText(rect, alignment | Qt.TextWordWrap, opt.text)
                painter.restore()
            else:
                text = opt.fontMetrics.elidedText(opt.text, Qt.ElideRight, max(0, rect.width()))
                painter.drawText(rect, alignment, text)
        painter.setPen(QPen(QColor(GRID_COLOR), 1))
        painter.drawLine(opt.rect.topRight(), opt.rect.bottomRight())
        painter.drawLine(opt.rect.bottomLeft(), opt.rect.bottomRight())
        if index.data(GROUP_START_ROLE):
            painter.setPen(QPen(QColor(GRID_COLOR), 1))
            painter.drawLine(opt.rect.topLeft(), opt.rect.topRight())
        painter.restore()

    def editorEvent(self, event, model, option, index):
        return False


class SelectAllHeader(QHeaderView):
    """Native indicator with the entire section retaining its click action."""
    def __init__(self, table, eligible=None):
        super().__init__(Qt.Horizontal, table)
        self.table = table
        self.eligible = eligible or (lambda item: bool(item.flags() & Qt.ItemIsUserCheckable))
        self.setSectionsClickable(True)
        for signal in (table.model().dataChanged, table.model().rowsInserted,
                       table.model().rowsRemoved, table.model().modelReset):
            signal.connect(lambda *_: self.viewport().update())

    def check_state(self):
        items = [self.table.item(row, 0) for row in range(self.table.rowCount())]
        items = [item for item in items if item is not None and self.eligible(item)]
        selected = sum(item.checkState() == Qt.Checked for item in items)
        if items and selected == len(items):
            return Qt.Checked
        return Qt.PartiallyChecked if selected else Qt.Unchecked

    def paintSection(self, painter, rect, section):
        if section != 0:
            super().paintSection(painter, rect, section)
            return
        painter.save()
        option = QStyleOptionHeader()
        self.initStyleOptionForIndex(option, section)
        option.rect = rect
        text = option.text
        option.text = ''
        self.style().drawControl(QStyle.CE_Header, option, painter, self)
        check = QStyleOptionButton()
        check.initFrom(self)
        indicator_widget = self if text else self.table
        indicator_style = indicator_widget.style()
        width = indicator_style.pixelMetric(QStyle.PM_IndicatorWidth, None, indicator_widget)
        height = indicator_style.pixelMetric(QStyle.PM_IndicatorHeight, None, indicator_widget)
        total = width + 5 + self.fontMetrics().horizontalAdvance(text)
        if text:
            left = rect.x() + max(4, (rect.width() - total) // 2)
        else:
            # QTableView excludes the grid pixel from visualRect, unlike the header.
            # Map the real body cell center into the header viewport, including scroll.
            body = self.table.visualRect(self.table.model().index(0, section))
            if body.isValid():
                center = self.viewport().mapFromGlobal(self.table.viewport().mapToGlobal(
                    QPoint(body.center().x(), 0))).x()
            else:
                body = rect.adjusted(0, 0, -int(self.table.showGrid()), 0)
                center = body.center().x()
            left = center - width // 2
        check.rect = QRect(left, rect.center().y() - height // 2, width, height)
        if not text:
            # Match the body indicator state as well as its primitive and metrics.
            check.state = QStyle.State_Enabled if self.table.isEnabled() else QStyle.State_None
            check.palette = self.table.palette()
        state = self.check_state()
        check.state |= {Qt.Checked: QStyle.State_On, Qt.Unchecked: QStyle.State_Off,
                        Qt.PartiallyChecked: QStyle.State_NoChange}[state]
        indicator_style.drawPrimitive(QStyle.PE_IndicatorCheckBox if text else
            QStyle.PE_IndicatorItemViewItemCheck, check, painter, indicator_widget)
        self.style().drawItemText(painter, QRect(left + width + 5, rect.y(),
            max(0, rect.right() - left - width - 8), rect.height()),
            Qt.AlignLeft | Qt.AlignVCenter, option.palette, self.isEnabled(), text,
            QPalette.ButtonText)
        painter.restore()


class ComicTable(QTableWidget):
    def __init__(self):
        super().__init__(0, 6)
        self._initial_widths = False
        self.check_column = 0
        self._check_pressed = None
        self._hover_index = None
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setItemDelegate(ComicDelegate(self))
        self.setHorizontalScrollBar(ReservedScrollBar(Qt.Horizontal, self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.horizontalHeader().setMinimumSectionSize(45)

    def mouseMoveEvent(self, event):
        self._hover_index = QPersistentModelIndex(self.indexAt(event.position().toPoint()))
        self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_index = None
        self.viewport().update()
        super().leaveEvent(event)

    def setCellWidget(self, row, column, widget):
        super().setCellWidget(row, column, widget)
        widget.setMouseTracking(True)
        widget.installEventFilter(self)

    def eventFilter(self, watched, event):
        if isinstance(watched, QWidget) and watched.parentWidget() == self.viewport():
            index = self.indexAt(watched.mapTo(self.viewport(), QPoint(1, 1)))
            if event.type() in (QEvent.Enter, QEvent.MouseMove):
                self._hover_index = QPersistentModelIndex(index)
                self.viewport().update()
            elif event.type() == QEvent.Leave:
                self._hover_index = None
                self.viewport().update()
            elif event.type() == QEvent.MouseButtonPress and index.isValid():
                self.setCurrentCell(index.row(), index.column(),
                    QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows)
        return super().eventFilter(watched, event)

    def enable_select_all_header(self, eligible=None):
        self.setHorizontalHeader(SelectAllHeader(self, eligible))
        self.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # Reserve room for both the indicator and label at the current font size.
        minimum = 45
        self.horizontalHeader().setMinimumSectionSize(45)
        self.horizontalHeader().sectionResized.connect(
            lambda section, old, new: self.setColumnWidth(0, minimum)
            if section == 0 and new < minimum else None)
        self.setColumnWidth(0, max(minimum, self.columnWidth(0)))

    def _toggle_check(self, index):
        if index.isValid() and index.flags() & Qt.ItemIsUserCheckable:
            item = self.item(index.row(),0)
            item.setCheckState(Qt.Unchecked if item.checkState()==Qt.Checked else Qt.Checked)
            return True
        return False

    def mousePressEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        self._check_pressed = QPersistentModelIndex(index) if event.button()==Qt.LeftButton and index.column()==0 else None
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        index = self.indexAt(event.position().toPoint())
        pressed,self._check_pressed = self._check_pressed,None
        super().mouseReleaseEvent(event)
        if event.button()==Qt.LeftButton and pressed is not None and pressed==index:
            self._toggle_check(index)

    def mouseDoubleClickEvent(self, event):
        if self.indexAt(event.position().toPoint()).column()==0:
            self._check_pressed = None
            event.accept()
        else:
            super().mouseDoubleClickEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Space,Qt.Key_Select) and self._toggle_check(self.model().index(self.currentRow(),0)):
            event.accept()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._initial_widths:
            return
        self._initial_widths = True
        metrics = self.fontMetrics()
        padding = 34 if isinstance(self.horizontalHeader(), SelectAllHeader) else 24
        label = self.horizontalHeaderItem(0).text() if self.horizontalHeaderItem(0) else ''
        fixed = [max(45, metrics.horizontalAdvance(label)+padding),
                 max(92, metrics.horizontalAdvance('审核失败')+24),
                 max(80, metrics.horizontalAdvance('CHANGE')+24)]
        remaining = max(360, self.viewport().width()-sum(fixed))
        title, suggested = max(120, int(remaining*.34)), max(120, int(remaining*.34))
        for column, width in enumerate((fixed[0], title, fixed[1], fixed[2], suggested, max(120, remaining-title-suggested))):
            self.setColumnWidth(column, width)


class ReviewTree(QTreeWidget):
    check_column = None

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hover_index = None
        self.setItemDelegate(ComicDelegate(self))
        self.setAlternatingRowColors(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setAllColumnsShowFocus(False)
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        palette = self.palette()
        palette.setColor(QPalette.Highlight, QColor(SELECTED_COLOR))
        palette.setColor(QPalette.HighlightedText, palette.color(QPalette.Text))
        self.setPalette(palette)

    def drawBranches(self, painter, rect, index):
        painter.fillRect(rect, cell_background(self, index, self.selectionModel().isSelected(index)))
        super().drawBranches(painter, rect, index)
        painter.setPen(QPen(QColor(GRID_COLOR), 1))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())

    def mouseMoveEvent(self, event):
        self._hover_index = QPersistentModelIndex(self.indexAt(event.position().toPoint()))
        self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self._hover_index = None
        self.viewport().update()
        super().leaveEvent(event)


class ReservedScrollBar(QScrollBar):
    def paintEvent(self, event):
        if self.maximum() > self.minimum():
            super().paintEvent(event)
        else:
            painter = QPainter(self)
            from PySide6.QtWidgets import QAbstractScrollArea
            parent = self.parentWidget()
            while parent is not None and not isinstance(parent,QAbstractScrollArea): parent = parent.parentWidget()
            surface = parent.viewport() if parent is not None else self
            painter.fillRect(self.rect(),surface.palette().color(surface.backgroundRole()))


def scroll_page(widget):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setVerticalScrollBar(ReservedScrollBar(Qt.Vertical, scroll))
    scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(widget)
    white_page(scroll)
    white_page(scroll.viewport())
    white_page(widget)
    return scroll


def section_line():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


class NoWheelSpinBox(QSpinBox):
    def wheelEvent(self, event):
        event.ignore()


class ValueWheelGuard(QObject):
    def eventFilter(self, watched, event):
        if event.type() == QEvent.Wheel and isinstance(watched, (QComboBox, QSpinBox)):
            event.ignore()
            return True
        return super().eventFilter(watched, event)


def guard_values(dialog):
    dialog._value_wheel_guard = ValueWheelGuard(dialog)
    for widget in dialog.findChildren(QComboBox) + dialog.findChildren(QSpinBox):
        widget.installEventFilter(dialog._value_wheel_guard)
