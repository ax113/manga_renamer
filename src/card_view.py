from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPoint, QPersistentModelIndex, QRect, QSize, Qt, QTimer, Signal
from PySide6.QtWidgets import QApplication, QAbstractItemView, QLineEdit, QListView, QRubberBand

from .card_delegate import MangaCardDelegate
from .card_model import CardListModel


class CardListView(QListView):
    currentItemChanged = Signal(object)
    checkedCountChanged = Signal()
    selectionReplaceRequested = Signal(object, bool)
    selectionToggleRequested = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setViewMode(QListView.IconMode)
        self.setFlow(QListView.TopToBottom)
        self.setWrapping(False)
        self.setMovement(QListView.Static)
        self.setResizeMode(QListView.Adjust)

        # V0.1.7 起不再使用 Qt 自带的橡皮框/selection 作为批量选择来源。
        # 自带橡皮框在“从卡片上开始拖动”时会把起点吸附到 item 左上角，
        # 还会受到上一次鼠标事件影响，造成框选起点错位。
        # 待执行状态统一由 model.item.checked 保存；框选由本类自己实现。
        self.setSelectionMode(QAbstractItemView.NoSelection)
        self.setDragDropMode(QListView.NoDragDrop)
        self.setSpacing(0)
        self.setUniformItemSizes(True)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setItemDelegate(MangaCardDelegate(self))
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setAutoScroll(False)
        self.verticalScrollBar().setSingleStep(18)

        self.clicked.connect(self._emit_current)

        # 自定义框选状态。
        self._rubber_band = QRubberBand(QRubberBand.Rectangle, self.viewport())
        self._drag_candidate = False
        self._drag_active = False
        self._press_pos = QPoint()
        self._press_content_y = 0
        self._last_drag_pos = QPoint()
        self._press_index = QPersistentModelIndex()
        self._press_on_edit_line = False
        self._press_on_selection_hot_area = False
        self._press_on_copy_line: int | None = None
        self._press_ctrl_click = False
        self._press_current_local_id: str | None = None

        # V0.2.7：前三行继续只创建一个按需出现的只读 QLineEdit 复制层，
        # 但不再要求“先点一下、第二次再拖选”。第一次在文字区域按下并拖动
        # 就直接进入文字选择；复制层始终只读，不会修改数据库字段或本地文件名。
        self._copy_editor = QLineEdit(self.viewport())
        self._copy_editor.setReadOnly(True)
        self._copy_editor.setFrame(False)
        self._copy_editor.setTextMargins(0, 0, 0, 0)
        self._copy_editor.setStyleSheet(
            "QLineEdit { border: 0px; background: transparent; padding: 0px; }"
        )
        self._copy_editor.hide()
        self._copy_overlay_row = -1
        self._copy_overlay_line = -1
        self._text_select_active = False
        self._text_select_anchor = 0
        self._edit_select_active = False
        self._edit_select_anchor = 0
        self._active_edit_editor: QLineEdit | None = None
        self._selection_before: set[int] = set()
        self._selection_additive = False
        self._range_anchor_row: int | None = None
        self.drag_resets_current_page_only = False

        # 固定速度的框选滚动，避免 Qt 默认 auto-scroll 出现忽快忽慢。
        self._drag_scroll_timer = QTimer(self)
        self._drag_scroll_timer.setInterval(30)
        self._drag_scroll_timer.timeout.connect(self._auto_scroll_tick)

        # 只读复制层不跟随滚动漂移：一旦滚动就收起，下次点击对应行再显示。
        self.verticalScrollBar().valueChanged.connect(self._hide_copy_editor)

    def setModel(self, model):
        self._hide_copy_editor()
        old = self.model()
        try:
            if old is not None:
                old.dataChanged.disconnect(self._on_model_data_changed)
                old.modelReset.disconnect(self._on_model_reset)
        except Exception:
            pass
        super().setModel(model)
        if model is not None:
            model.dataChanged.connect(self._on_model_data_changed)
            model.modelReset.connect(self._on_model_reset)

    def _on_model_reset(self):
        self._range_anchor_row = None
        self._drag_scroll_timer.stop()
        self._rubber_band.hide()
        self._reset_drag_state()
        self._hide_copy_editor()
        self.clear_current_item()

    def reset_range_anchor(self):
        self._range_anchor_row = None

    def commit_active_edit(self):
        """Submit an active name editor before paging/navigation/session save."""
        editor = self._find_active_edit_editor()
        if editor is None:
            return
        editor.clearFocus()
        QApplication.processEvents()

    def set_drag_resets_current_page_only(self, enabled: bool):
        self.drag_resets_current_page_only = bool(enabled)

    def _on_model_data_changed(self, *args):
        self.checkedCountChanged.emit()

    def resizeEvent(self, event):
        self._hide_copy_editor()
        super().resizeEvent(event)
        self.setGridSize(QSize(max(1, self.viewport().width() - 8), MangaCardDelegate.CARD_HEIGHT))

    def _hide_copy_editor(self, *args):
        if not hasattr(self, "_copy_editor"):
            return
        old_row = self._copy_overlay_row
        self._text_select_active = False
        self._copy_editor.hide()
        self._copy_editor.clearFocus()
        self._copy_overlay_row = -1
        self._copy_overlay_line = -1
        if old_row >= 0 and self.model() is not None and old_row < self.model().rowCount():
            idx = self.model().index(old_row, 0)
            rect = self.visualRect(idx)
            if rect.isValid():
                self.viewport().update(rect)

    def _begin_copy_line(self, persistent: QPersistentModelIndex, line: int, click_pos: QPoint) -> int | None:
        if not persistent.isValid() or line not in (0, 1, 2):
            return None
        index = QModelIndex(persistent)
        delegate = self.itemDelegate()
        if not isinstance(delegate, MangaCardDelegate):
            return None

        item = index.data(CardListModel.ITEM_ROLE)
        values = (
            index.data(CardListModel.TITLE_JPN_ROLE) or "",
            index.data(CardListModel.TITLE_ROLE) or "",
            index.data(CardListModel.FILE_NAME_ROLE) or "",
        )
        text = values[line]
        if not text:
            self._hide_copy_editor()
            return None

        rect = self.visualRect(index)
        if not rect.isValid():
            return None
        value_rect = delegate._readonly_value_rect(rect, line, item)

        # 先登记覆盖行并刷新，让 delegate 不再重复画同一行文字。复制层
        # 从第一次 mousePress 就出现，因此不会再经历“先绘制文本 -> 第二次点击
        # 才切换控件”的两阶段交互。
        self._copy_overlay_row = index.row()
        self._copy_overlay_line = line
        self.viewport().update(rect)

        self._copy_editor.setFont(self.font())
        overlay_left = delegate._label_visual_right(rect, line) + 1
        overlay_rect = QRect(
            overlay_left,
            value_rect.top(),
            max(1, value_rect.right() - overlay_left + 1),
            value_rect.height(),
        )
        # QLineEdit 即使无边框，文本绘制仍会比 delegate 文字自然右移约 2px。
        # 用内部 textMargin 反向补偿，同时把“冒号 -> 正文”的空白纳入首次命中区。
        left_margin = max(0, value_rect.left() - overlay_left - delegate.QLINE_TEXT_COMPENSATION)
        self._copy_editor.setTextMargins(left_margin, 0, 0, 0)
        self._copy_editor.setGeometry(overlay_rect)
        self._copy_editor.setText(text)
        self._copy_editor.show()
        self._copy_editor.raise_()
        self._copy_editor.setFocus(Qt.MouseFocusReason)

        # 点击坐标必须相对实际覆盖 QLineEdit，而不是正文 value_rect。
        # 否则从冒号后的左侧空白按下时，首次光标/拖选锚点会发生偏差。
        local = click_pos - self._copy_editor.geometry().topLeft()
        try:
            cursor_pos = self._copy_editor.cursorPositionAt(local)
        except Exception:
            cursor_pos = len(text)
        cursor_pos = max(0, min(len(text), cursor_pos))
        self._copy_editor.deselect()
        self._copy_editor.setCursorPosition(cursor_pos)
        return cursor_pos

    def _update_first_drag_text_selection(self, pos: QPoint):
        """处理第一次按下后尚未交给 QLineEdit 的拖选过程。

        第一次 mousePress 发生在 viewport 上，所以本次拖动由 view 手动更新
        QLineEdit 的 selection；松开后复制层保留，之后的拖选/双击/右键复制
        都会直接由 QLineEdit 自己处理。
        """
        if not self._text_select_active or not self._copy_editor.isVisible():
            return
        local = pos - self._copy_editor.geometry().topLeft()
        try:
            cursor_pos = self._copy_editor.cursorPositionAt(local)
        except Exception:
            cursor_pos = self._text_select_anchor
        cursor_pos = max(0, min(len(self._copy_editor.text()), cursor_pos))
        anchor = self._text_select_anchor
        if cursor_pos >= anchor:
            self._copy_editor.setSelection(anchor, cursor_pos - anchor)
        else:
            self._copy_editor.setSelection(cursor_pos, anchor - cursor_pos)

    def _find_active_edit_editor(self) -> QLineEdit | None:
        for editor in self.viewport().findChildren(QLineEdit):
            if editor is self._copy_editor:
                continue
            if editor.isVisible() and not editor.isReadOnly():
                return editor
        return None

    def _begin_edit_at(self, persistent: QPersistentModelIndex, click_pos: QPoint) -> bool:
        """第一次按下建议名时立即进入编辑，并把同一次点击用于光标定位。"""
        if not persistent.isValid():
            return False
        index = QModelIndex(persistent)
        self._set_current_item(index)
        self.edit(index)
        editor = self._find_active_edit_editor()
        if editor is None:
            return False
        editor.setFocus(Qt.MouseFocusReason)
        local = click_pos - editor.geometry().topLeft()
        try:
            cursor_pos = editor.cursorPositionAt(local)
        except Exception:
            cursor_pos = len(editor.text())
        cursor_pos = max(0, min(len(editor.text()), cursor_pos))
        editor.deselect()
        editor.setCursorPosition(cursor_pos)
        self._active_edit_editor = editor
        self._edit_select_active = True
        self._edit_select_anchor = cursor_pos
        return True

    def _update_first_drag_edit_selection(self, pos: QPoint):
        editor = self._active_edit_editor
        if not self._edit_select_active or editor is None or not editor.isVisible():
            return
        local = pos - editor.geometry().topLeft()
        try:
            cursor_pos = editor.cursorPositionAt(local)
        except Exception:
            cursor_pos = self._edit_select_anchor
        cursor_pos = max(0, min(len(editor.text()), cursor_pos))
        anchor = self._edit_select_anchor
        if cursor_pos >= anchor:
            editor.setSelection(anchor, cursor_pos - anchor)
        else:
            editor.setSelection(cursor_pos, anchor - cursor_pos)

    def _set_current_item(self, index: QModelIndex):
        """只改变“当前查看项”，不影响待执行勾选状态。"""
        old = self.currentIndex()
        self.setCurrentIndex(index)
        # 自定义 delegate 不使用 Qt selection 背景，因此显式刷新旧/新卡片，
        # 保证当前查看项的高亮能立即从上一项移动到下一项。
        if old.isValid():
            self.viewport().update(self.visualRect(old))
        if index.isValid():
            self.viewport().update(self.visualRect(index))

    def clear_current_item(self):
        old = self.currentIndex()
        self.setCurrentIndex(QModelIndex())
        if old.isValid():
            self.viewport().update(self.visualRect(old))

    def _checked_rows(self) -> set[int]:
        model = self.model()
        if isinstance(model, CardListModel):
            return set(model.checked_rows())
        return set()

    def _toggle_check(self, index: QModelIndex):
        model = self.model()
        if not isinstance(model, CardListModel):
            return
        checked = bool(index.data(CardListModel.CHECKED_ROLE))
        model.setData(index, Qt.Unchecked if checked else Qt.Checked, Qt.CheckStateRole)
        self.checkedCountChanged.emit()

    def _set_checked_target(self, rows: set[int], additive: bool):
        model = self.model()
        if not isinstance(model, CardListModel):
            return

        target = (self._selection_before | rows) if additive else rows
        for row, item in enumerate(model.items):
            desired = row in target
            if item.checked != desired:
                model.set_checked(row, desired)
        self.checkedCountChanged.emit()

    def _begin_drag_candidate(
        self,
        pos: QPoint,
        index: QModelIndex,
        mods,
        on_edit_line: bool,
        on_selection_hot_area: bool = False,
        on_copy_line: int | None = None,
    ):
        self._drag_candidate = True
        self._drag_active = False
        self._press_pos = QPoint(pos)
        self._last_drag_pos = QPoint(pos)
        self._press_content_y = pos.y() + self.verticalScrollBar().value()
        self._press_index = QPersistentModelIndex(index) if index.isValid() else QPersistentModelIndex()
        self._press_on_edit_line = on_edit_line
        self._press_on_selection_hot_area = on_selection_hot_area
        self._press_on_copy_line = on_copy_line
        self._press_ctrl_click = bool(mods & Qt.ControlModifier)
        self._selection_before = self._checked_rows()
        self._selection_additive = bool(mods & Qt.ControlModifier)
        current = self.currentIndex()
        current_item = current.data(CardListModel.ITEM_ROLE) if current.isValid() else None
        self._press_current_local_id = current_item.local_id if current_item is not None else None

    def _restore_press_current(self):
        """Qt may change currentIndex during a press; the drag only changes checks."""
        model = self.model()
        if isinstance(model, CardListModel) and self._press_current_local_id is not None:
            for row, item in enumerate(model.items):
                if item.local_id == self._press_current_local_id:
                    index = model.index(row, 0)
                    if self.currentIndex() != index:
                        self._set_current_item(index)
                    return
        if self.currentIndex().isValid():
            self.clear_current_item()

    def mousePressEvent(self, event):
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        mods = event.modifiers()

        if event.button() == Qt.LeftButton:
            self._hide_copy_editor()
            # V0.1.8.1：所有“单击动作”延迟到 mouseReleaseEvent 决定。
            # V0.2.7 唯一例外是前三行文字：第一次按下就建立只读复制层，
            # 从第一次拖动即可直接选字。
            if index.isValid():
                rect = self.visualRect(index)
                delegate = self.itemDelegate()
                on_hot_area = isinstance(delegate, MangaCardDelegate) and delegate._selection_hot_rect(rect).contains(pos)

                on_edit_line = isinstance(delegate, MangaCardDelegate) and delegate._second_line_rect(rect).contains(pos)
                on_copy_line = (
                    delegate._copy_line_at(rect, pos, index.data(CardListModel.ITEM_ROLE))
                    if isinstance(delegate, MangaCardDelegate)
                    else None
                )

                # Shift 只改变当前页紫色选择，绝不改变金色当前项。
                if mods & Qt.ShiftModifier:
                    anchor = self._range_anchor_row
                    if anchor is None:
                        anchor = index.row()
                    lo, hi = sorted((anchor, index.row()))
                    rows = set(range(lo, hi + 1))
                    self._selection_before = self._checked_rows()
                    self._set_checked_target(rows, additive=bool(mods & Qt.ControlModifier))
                    self._range_anchor_row = index.row()
                    event.accept()
                    return

                # 第四行建议名：第一次 mousePress 就创建编辑器并定位光标。
                # 同一次按住拖动也直接用于选字，不再要求先点一下“激活”。
                if on_edit_line and not on_hot_area and not (mods & Qt.ControlModifier):
                    persistent = QPersistentModelIndex(index)
                    if self._begin_edit_at(persistent, pos):
                        self._emit_current(index)
                        self._range_anchor_row = index.row()
                        event.accept()
                        return

                # 保留 Ctrl+单击卡片切换勾选的旧语义；普通左键在前三行
                # 实际文字 + 2 字符缓冲区内，则优先进入文字选择。
                if (
                    on_copy_line is not None
                    and not on_hot_area
                    and not on_edit_line
                    and not (mods & Qt.ControlModifier)
                ):
                    persistent = QPersistentModelIndex(index)
                    anchor = self._begin_copy_line(persistent, on_copy_line, pos)
                    if anchor is not None:
                        self._set_current_item(index)
                        self._emit_current(index)
                        self._range_anchor_row = index.row()
                        self._text_select_active = True
                        self._text_select_anchor = anchor
                        event.accept()
                        return

                self._begin_drag_candidate(
                    pos,
                    index,
                    mods,
                    on_edit_line,
                    on_selection_hot_area=on_hot_area,
                    on_copy_line=None,
                )
                event.accept()
                return

            # 空白处也允许从精确鼠标位置开始框选；Ctrl 可在按下前就保持。
            self._begin_drag_candidate(pos, QModelIndex(), mods, False)
            event.accept()
            return

        # 非左键完全交回 Qt；它不会再影响我们自己的框选起点。
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._edit_select_active and (event.buttons() & Qt.LeftButton):
            self._update_first_drag_edit_selection(event.position().toPoint())
            event.accept()
            return

        if self._text_select_active and (event.buttons() & Qt.LeftButton):
            self._update_first_drag_text_selection(event.position().toPoint())
            event.accept()
            return

        if self._drag_candidate and (event.buttons() & Qt.LeftButton):
            pos = event.position().toPoint()
            self._last_drag_pos = QPoint(pos)
            # Ctrl 可在开始框选前按住，也可以在拖动过程中再按下。
            # 一旦本次框选检测到 Ctrl，就采用“追加勾选”语义，
            # 保留框选开始前已经勾选的项目。
            if event.modifiers() & Qt.ControlModifier:
                self._selection_additive = True

            if not self._drag_active:
                distance = (pos - self._press_pos).manhattanLength()
                if distance >= QApplication.startDragDistance():
                    self._drag_active = True
                    self._restore_press_current()
                    self._rubber_band.show()

            if self._drag_active:
                self._update_rubber_band()
                if pos.y() < 0 or pos.y() > self.viewport().height():
                    if not self._drag_scroll_timer.isActive():
                        self._drag_scroll_timer.start()
                else:
                    self._drag_scroll_timer.stop()
                event.accept()
                return

            # Before the drag threshold Qt must not interpret motion as a
            # default item-view press and move currentIndex to the start card.
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self._edit_select_active:
            self._update_first_drag_edit_selection(event.position().toPoint())
            self._edit_select_active = False
            self._active_edit_editor = None
            event.accept()
            return

        if event.button() == Qt.LeftButton and self._text_select_active:
            self._update_first_drag_text_selection(event.position().toPoint())
            self._text_select_active = False
            event.accept()
            return

        if event.button() == Qt.LeftButton and self._drag_candidate:
            self._drag_scroll_timer.stop()

            if self._drag_active:
                self._last_drag_pos = event.position().toPoint()
                rows = self._rows_inside_drag_rect()
                # 松开时仍检查一次 Ctrl，兼容“拖动途中才按 Ctrl”的习惯。
                additive = self._selection_additive or bool(event.modifiers() & Qt.ControlModifier)
                # 避免一次完全落在空白处的误拖动把既有勾选全部清空。
                if rows:
                    model = self.model()
                    local_ids = {
                        model.items[row].local_id
                        for row in rows
                        if isinstance(model, CardListModel) and 0 <= row < len(model.items)
                    }
                    if additive:
                        self.selectionToggleRequested.emit(local_ids)
                    else:
                        self.selectionReplaceRequested.emit(
                            local_ids,
                            bool(self.drag_resets_current_page_only),
                        )
                self._restore_press_current()
                self._rubber_band.hide()
                self._reset_drag_state()
                event.accept()
                return

            # 没有形成拖框时，才把本次操作解释成“单击”。
            if self._press_index.isValid():
                index = QModelIndex(self._press_index)
                self._range_anchor_row = index.row()

                # 左侧选择热区点击，或 Ctrl+单击卡片：切换当前项勾选。
                # 由于动作发生在释放阶段，因此 Ctrl+按下后直接拖动不会被阻断。
                if self._press_on_selection_hot_area or self._press_ctrl_click:
                    self._toggle_check(index)
                    self._restore_press_current()
                elif self._press_on_edit_line:
                    persistent = QPersistentModelIndex(index)
                    QTimer.singleShot(0, lambda p=persistent: self._begin_edit(p))
                elif self._press_on_copy_line is not None:
                    persistent = QPersistentModelIndex(index)
                    line = self._press_on_copy_line
                    click_pos = QPoint(self._press_pos)
                    QTimer.singleShot(
                        0,
                        lambda p=persistent, ln=line, cp=click_pos: self._begin_copy_line(p, ln, cp),
                    )
                else:
                    # 只有普通卡片主体单击改变金色当前项；勾选、Ctrl、Shift、拖框都不会。
                    self._set_current_item(index)
                    self._emit_current(index)
            self._reset_drag_state()
            event.accept()
            return

        super().mouseReleaseEvent(event)

    def _reset_drag_state(self):
        self._drag_candidate = False
        self._drag_active = False
        self._press_index = QPersistentModelIndex()
        self._press_on_edit_line = False
        self._press_on_selection_hot_area = False
        self._press_on_copy_line = None
        self._press_ctrl_click = False
        self._press_current_local_id = None
        self._selection_before = set()
        self._selection_additive = False

    def _current_content_point(self) -> QPoint:
        return QPoint(
            self._last_drag_pos.x(),
            self._last_drag_pos.y() + self.verticalScrollBar().value(),
        )

    def _content_drag_rect(self) -> QRect:
        current = self._current_content_point()
        origin = QPoint(self._press_pos.x(), self._press_content_y)
        return QRect(origin, current).normalized()

    def _update_rubber_band(self):
        if not self._drag_active:
            return
        scroll = self.verticalScrollBar().value()
        origin_view = QPoint(self._press_pos.x(), self._press_content_y - scroll)
        current_view = QPoint(
            max(0, min(self.viewport().width(), self._last_drag_pos.x())),
            max(0, min(self.viewport().height(), self._last_drag_pos.y())),
        )
        self._rubber_band.setGeometry(QRect(origin_view, current_view).normalized())

    def _rows_inside_drag_rect(self) -> set[int]:
        model = self.model()
        if not isinstance(model, CardListModel):
            return set()

        drag_rect = self._content_drag_rect()
        scroll = self.verticalScrollBar().value()
        rows: set[int] = set()
        for row in range(model.rowCount()):
            index = model.index(row, 0)
            rect = self.visualRect(index)
            if not rect.isValid():
                continue
            content_rect = rect.translated(0, scroll)
            if drag_rect.intersects(content_rect):
                rows.add(row)
        return rows

    def _auto_scroll_tick(self):
        if not self._drag_active:
            self._drag_scroll_timer.stop()
            return

        bar = self.verticalScrollBar()
        old_value = bar.value()
        step = 18  # 固定像素速度，避免越界后突然加速/忽慢忽快。
        if self._last_drag_pos.y() < 0:
            bar.setValue(old_value - step)
        elif self._last_drag_pos.y() > self.viewport().height():
            bar.setValue(old_value + step)
        else:
            self._drag_scroll_timer.stop()
            return

        if bar.value() == old_value:
            # 已到顶部/底部。
            self._drag_scroll_timer.stop()
        self._update_rubber_band()

    def _begin_edit(self, persistent: QPersistentModelIndex):
        if not persistent.isValid():
            return
        index = QModelIndex(persistent)
        self._set_current_item(index)
        self.edit(index)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_F2, Qt.Key_Return, Qt.Key_Enter):
            index = self.currentIndex()
            if index.isValid():
                self.edit(index)
                return
        if event.key() == Qt.Key_Space:
            index = self.currentIndex()
            if index.isValid():
                self._toggle_check(index)
                return
        super().keyPressEvent(event)

    def _emit_current(self, index: QModelIndex):
        if not index.isValid():
            return
        item = index.data(index.model().ITEM_ROLE)
        self.currentItemChanged.emit(item)
