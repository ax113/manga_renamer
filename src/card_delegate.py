from __future__ import annotations

from PySide6.QtCore import QModelIndex, QPersistentModelIndex, QRect, QSize, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette, QPen
from PySide6.QtWidgets import QApplication, QLineEdit, QStyle, QStyleOptionButton, QStyledItemDelegate

from .card_model import CardListModel
from .file_operations import file_labels


class MangaCardDelegate(QStyledItemDelegate):
    # V0.2.9：行高固定 23px；后续不再继续压缩。
    CARD_HEIGHT = 114
    OUTER_MARGIN = 5
    CHECKBOX_AREA_WIDTH = 32
    TEXT_INSET = 8
    ROW_HEIGHT = 23
    ROW_GAP = 0
    TOP_PADDING = 5
    TAG_TOP_OFFSET = -1
    COPY_BUFFER_CHARS = 2
    QLINE_TEXT_COMPENSATION = 2

    LABELS = ("标题：", "英文标题：", "文件名：", "建议名：")
    TAG_LABELS = ("语言：", "团体：", "作者：")
    TAG_BLOCK_MIN_WIDTH = 180
    TAG_BLOCK_MAX_WIDTH = 260
    TAG_BLOCK_GAP = 12

    @staticmethod
    def file_badge(item):
        if item is None:
            return ''
        return ' · '.join(file_labels(item)) or (item.file_status if item.file_status in {'已撤销', '已撤销移动'} else '')

    def sizeHint(self, option, index):
        return QSize(max(1, option.rect.width()), self.CARD_HEIGHT)

    def _content_rect(self, rect: QRect) -> QRect:
        return rect.adjusted(self.OUTER_MARGIN, 4, -self.OUTER_MARGIN, -4)

    def _checkbox_rect(self, rect: QRect) -> QRect:
        c = self._content_rect(rect)
        # 只微调视觉位置：比 V0.2.5 向左 4px。勾选逻辑与左侧热区保持不变。
        return QRect(c.left() + 6, c.top() + max(0, (c.height() - 20) // 2), 20, 20)

    def _selection_hot_rect(self, rect: QRect) -> QRect:
        """卡片左侧整块选择热区，保持旧版行为。"""
        c = self._content_rect(rect)
        right = max(c.left(), self._text_left(rect) - 1)
        return QRect(c.left(), c.top(), max(1, right - c.left() + 1), c.height())

    def _text_left(self, rect: QRect) -> int:
        c = self._content_rect(rect)
        return c.left() + self.CHECKBOX_AREA_WIDTH

    def _label_metrics(self) -> tuple[int, int]:
        """返回四个标签所需宽度与约半个汉字的间距。"""
        font = QFont(QApplication.font())
        font.setBold(True)
        fm = QFontMetrics(font)
        label_width = max(fm.horizontalAdvance(label) for label in self.LABELS)
        normal_fm = QFontMetrics(QApplication.font())
        half_cjk = max(4, round(normal_fm.horizontalAdvance("汉") * 0.5))
        return label_width, half_cjk

    def _value_text_left(self, rect: QRect) -> int:
        label_width, gap = self._label_metrics()
        return self._text_left(rect) + label_width + gap

    def _tag_block_width(self, rect: QRect) -> int:
        c = self._content_rect(rect)
        # 极窄窗口优先保证标题/文件名仍可读；正常宽度时右侧稳定显示三行 Tag。
        if c.width() < 560:
            return 0
        return max(self.TAG_BLOCK_MIN_WIDTH, min(self.TAG_BLOCK_MAX_WIDTH, round(c.width() * 0.26)))

    def _tag_block_rect(self, rect: QRect) -> QRect:
        c = self._content_rect(rect)
        width = self._tag_block_width(rect)
        if width <= 0:
            return QRect()
        return QRect(c.right() - width + 1, c.top(), width, c.height())

    def _tag_label_width(self) -> int:
        font = QFont(QApplication.font())
        font.setBold(True)
        fm = QFontMetrics(font)
        return max(fm.horizontalAdvance(label) for label in self.TAG_LABELS)

    def _tag_label_rect(self, rect: QRect, row: int) -> QRect:
        block = self._tag_block_rect(rect)
        if block.isNull():
            return QRect()
        rr = self._row_rect(rect, row).translated(0, self.TAG_TOP_OFFSET)
        width = self._tag_label_width()
        return QRect(block.left(), rr.top(), width, rr.height())

    def _tag_value_rect(self, rect: QRect, row: int) -> QRect:
        block = self._tag_block_rect(rect)
        if block.isNull():
            return QRect()
        rr = self._row_rect(rect, row).translated(0, self.TAG_TOP_OFFSET)
        label_width = self._tag_label_width()
        left = block.left() + label_width + 4
        return QRect(left, rr.top(), max(1, block.right() - left + 1), rr.height())

    def _row_rect(self, rect: QRect, row: int) -> QRect:
        c = self._content_rect(rect)
        top = c.top() + self.TOP_PADDING + row * (self.ROW_HEIGHT + self.ROW_GAP)
        return QRect(c.left(), top, c.width(), self.ROW_HEIGHT)

    def _label_rect(self, rect: QRect, row: int) -> QRect:
        label_width, _ = self._label_metrics()
        r = self._row_rect(rect, row)
        return QRect(self._text_left(rect), r.top(), label_width, r.height())

    def _label_visual_right(self, rect: QRect, row: int) -> int:
        """估算右对齐字段标签中最后一个可见墨迹像素的 X 坐标。

        复制热区从这个位置的下一像素开始，而不是从正文矩形开始，
        这样“冒号 -> 正文”之间的空白也能直接作为第一次拖选起点。
        """
        label_rect = self._label_rect(rect, row)
        text = self.LABELS[row]
        font = QFont(QApplication.font())
        font.setBold(True)
        fm = QFontMetrics(font)
        advance = fm.horizontalAdvance(text)
        tight = fm.tightBoundingRect(text)
        origin_x = label_rect.right() + 1 - advance
        return min(label_rect.right(), origin_x + tight.right())

    def _readonly_value_rect(self, rect: QRect, line: int, item=None) -> QRect:
        """前三行只读正文区域。

        这些区域仍由 delegate 绘制；用户单击后 CardListView 会在同一区域覆盖一个
        borderless/read-only QLineEdit，从而可以选择、Ctrl+C 或右键复制，而不能修改。
        """
        c = self._content_rect(rect)
        row = self._row_rect(rect, line)
        left = self._value_text_left(rect)
        tag_width = self._tag_block_width(rect)
        if tag_width > 0:
            right = c.right() - tag_width - self.TAG_BLOCK_GAP
        else:
            right = c.right() - 18
        width = max(20, right - left + 1)
        return QRect(left, row.top(), width, row.height())

    def _readonly_text_value(self, item, line: int) -> str:
        if item is None or line not in (0, 1, 2):
            return ""
        if line == 0:
            return item.record.title_jpn if item.record else ""
        if line == 1:
            return item.record.title if item.record else ""
        return f"{item.original_name}{item.suffix}"

    def _readonly_text_hit_rect(self, rect: QRect, line: int, item=None) -> QRect:
        """前三行真正用于“文字选择”的横向命中区。

        命中宽度按当前实际显示文字计算，并在文字末尾额外保留约 2 个汉字宽度
        的缓冲区；缓冲区之后立即恢复为卡片空白，可作为橡皮框起点。
        对于本身已经长到需要省略号的文字，命中区最多占满正文显示区域。
        """
        full = self._readonly_value_rect(rect, line, item)
        text = self._readonly_text_value(item, line)
        if not text or full.isNull():
            return QRect()

        font = QFont(self.parent().font() if self.parent() is not None else QApplication.font())
        fm = QFontMetrics(font)
        display = fm.elidedText(text, Qt.ElideRight, full.width())
        text_width = fm.horizontalAdvance(display)
        buffer_width = max(1, fm.horizontalAdvance("汉") * self.COPY_BUFFER_CHARS)

        # 左侧选择区从“字段标签冒号最后一个可见像素”后立即开始。
        # 标签与正文之间原有的空白全部算作正文选择缓冲区；右侧仍保留 2 字符缓冲。
        hit_left = self._label_visual_right(rect, line) + 1
        hit_right = min(full.right(), full.left() + text_width + buffer_width - 1)
        return QRect(hit_left, full.top(), max(1, hit_right - hit_left + 1), full.height())

    def _copy_line_at(self, rect: QRect, pos, item=None) -> int | None:
        """返回鼠标所在的前三行文字选择区编号。

        与 V0.2.6 的“整段正文区域都算复制区”不同，V0.2.7 只覆盖实际文字
        加 2 字符缓冲区，后方空白仍可直接开始框选。
        """
        for line in range(3):
            if self._readonly_text_hit_rect(rect, line, item).contains(pos):
                return line
        return None

    def _second_line_rect(self, rect: QRect) -> QRect:
        """兼容旧调用名：现在实际是第 4 行“建议名”的编辑框区域。"""
        c = self._content_rect(rect)
        row = self._row_rect(rect, 3)
        value_text_left = self._value_text_left(rect)
        # 前三行正文与编辑框内部文字保持同一 X 起点。
        editor_left = value_text_left - self.TEXT_INSET
        width = max(40, c.right() - editor_left - 9)
        return QRect(editor_left, row.top(), width, row.height())

    def _alternating_background(self, palette: QPalette, row: int) -> QColor:
        base = palette.color(QPalette.Base)
        if base.lightness() >= 128:
            return QColor(234, 245, 255) if row % 2 == 0 else QColor(214, 234, 252)
        return QColor(31, 48, 65) if row % 2 == 0 else QColor(38, 59, 79)

    def paint(self, painter: QPainter, option, index):
        painter.save()
        palette = option.palette
        card = self._content_rect(option.rect)

        checked = bool(index.data(CardListModel.CHECKED_ROLE))
        view = self.parent()
        try:
            is_current = bool(view is not None and view.currentIndex().isValid() and view.currentIndex() == index)
        except Exception:
            is_current = False

        bg = self._alternating_background(palette, index.row())
        border = QColor(126, 157, 184) if palette.color(QPalette.Base).lightness() >= 128 else palette.color(QPalette.Mid)
        border_width = 1
        if checked:
            if palette.color(QPalette.Base).lightness() >= 128:
                bg = QColor(242, 238, 250)
            else:
                bg = QColor(66, 57, 82)
            border = palette.color(QPalette.Highlight)
            border_width = 2

        if is_current:
            if not checked:
                if palette.color(QPalette.Base).lightness() >= 128:
                    bg = QColor(255, 249, 224)
                else:
                    bg = QColor(72, 62, 37)
            border = QColor(219, 157, 39) if palette.color(QPalette.Base).lightness() >= 128 else QColor(236, 185, 72)
            border_width = 3

        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setBrush(bg)
        painter.setPen(QPen(border, border_width))
        painter.drawRoundedRect(card.adjusted(0, 0, -1, -1), 6, 6)
        if is_current:
            accent = QColor(219, 157, 39) if palette.color(QPalette.Base).lightness() >= 128 else QColor(236, 185, 72)
            painter.setPen(Qt.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(QRect(card.left() + 2, card.top() + 6, 4, max(8, card.height() - 12)), 2, 2)

        item = index.data(CardListModel.ITEM_ROLE)
        title_jpn = index.data(CardListModel.TITLE_JPN_ROLE) or ""
        title = index.data(CardListModel.TITLE_ROLE) or ""
        file_name = index.data(CardListModel.FILE_NAME_ROLE) or ""
        language_tags = index.data(CardListModel.LANGUAGE_TAGS_ROLE) or ""
        group_tags = index.data(CardListModel.GROUP_TAGS_ROLE) or ""
        artist_tags = index.data(CardListModel.ARTIST_TAGS_ROLE) or ""
        suggested = index.data(CardListModel.SUGGESTED_ROLE) or ""
        manual = bool(index.data(CardListModel.MANUAL_ROLE))
        name_source = index.data(CardListModel.NAME_SOURCE_ROLE) or "program"
        manual_confirmed = bool(index.data(CardListModel.MANUAL_CONFIRMED_ROLE))
        ai_status = index.data(CardListModel.AI_REVIEWED_ROLE) or "AI未审"
        attributes = index.data(CardListModel.ATTRIBUTES_ROLE) or ""

        checkbox = QStyleOptionButton()
        checkbox.rect = self._checkbox_rect(option.rect)
        checkbox.state = QStyle.State_Enabled
        checkbox.state |= QStyle.State_On if checked else QStyle.State_Off
        QApplication.style().drawControl(QStyle.CE_CheckBox, checkbox, painter)

        text_color = palette.color(QPalette.Text)
        placeholder_color = palette.color(QPalette.PlaceholderText)
        label_font = QFont(option.font)
        label_font.setBold(True)
        normal_font = QFont(option.font)

        # 前三行严格沿用上游 exhentai-manga-manager 的语义：
        # 标题=title_jpn，英文标题=title，文件名=本地文件夹名/压缩包名。
        values = (title_jpn, title, file_name)
        overlay_row = getattr(view, "_copy_overlay_row", -1) if view is not None else -1
        overlay_line = getattr(view, "_copy_overlay_line", -1) if view is not None else -1

        for line, value in enumerate(values):
            painter.setPen(text_color)
            painter.setFont(label_font)
            painter.drawText(
                self._label_rect(option.rect, line),
                Qt.AlignRight | Qt.AlignVCenter | Qt.TextSingleLine,
                self.LABELS[line],
            )

            # 只读 QLineEdit 覆盖时不重复绘制正文，避免文字叠加变粗。
            if overlay_row == index.row() and overlay_line == line:
                continue

            value_rect = self._readonly_value_rect(option.rect, line, item)
            painter.setFont(normal_font)
            if value:
                painter.setPen(text_color)
                display = painter.fontMetrics().elidedText(value, Qt.ElideRight, value_rect.width())
            else:
                painter.setPen(placeholder_color)
                display = "(空)"
            painter.save()
            painter.setClipRect(value_rect, Qt.IntersectClip)
            painter.drawText(value_rect, Qt.AlignVCenter | Qt.TextSingleLine, display)
            painter.restore()

        # 右侧前三行显示数据库原始英文 Tag。路径匹配方式不再占用主卡片，
        # 仍可在详情面板的“匹配方式”字段中查看。
        if self._tag_block_width(option.rect) > 0:
            tag_values = (language_tags, group_tags, artist_tags)
            for line, value in enumerate(tag_values):
                label_rect = self._tag_label_rect(option.rect, line)
                value_rect = self._tag_value_rect(option.rect, line)
                painter.setFont(label_font)
                painter.setPen(text_color)
                painter.drawText(
                    label_rect,
                    Qt.AlignRight | Qt.AlignVCenter | Qt.TextSingleLine,
                    self.TAG_LABELS[line],
                )
                painter.setFont(normal_font)
                if value:
                    painter.setPen(text_color)
                    display = painter.fontMetrics().elidedText(value, Qt.ElideRight, value_rect.width())
                else:
                    painter.setPen(placeholder_color)
                    display = "(无)"
                painter.save()
                painter.setClipRect(value_rect, Qt.IntersectClip)
                painter.drawText(value_rect, Qt.AlignVCenter | Qt.TextSingleLine, display)
                painter.restore()

        # 第四行：建议名，可编辑逻辑完全沿用旧版。
        edit_rect = self._second_line_rect(option.rect)
        painter.setPen(text_color)
        painter.setFont(label_font)
        painter.drawText(
            self._label_rect(option.rect, 3),
            Qt.AlignRight | Qt.AlignVCenter | Qt.TextSingleLine,
            self.LABELS[3],
        )

        painter.setBrush(palette.color(QPalette.Base))
        painter.setPen(QPen(palette.color(QPalette.Mid), 1))
        painter.drawRoundedRect(edit_rect.adjusted(0, 0, -1, -1), 4, 4)
        painter.setFont(normal_font)
        painter.setPen(text_color)
        available = max(1, edit_rect.width() - self.TEXT_INSET * 2)
        suggested_text = painter.fontMetrics().elidedText(suggested, Qt.ElideRight, available)
        painter.drawText(
            edit_rect.adjusted(self.TEXT_INSET, 0, -self.TEXT_INSET, 0),
            Qt.AlignVCenter | Qt.TextSingleLine,
            suggested_text,
        )

        # 搜索结果总览用同一个 WorkItem，只额外显示其真实来源分类。
        if bool(getattr(view, "is_search_results_view", False)) and card.width() >= 520:
            category = index.data(CardListModel.CATEGORY_ROLE) or ""
            if category:
                painter.setFont(normal_font)
                painter.setPen(palette.color(QPalette.PlaceholderText))
                badge_rect = QRect(card.right() - 420, card.bottom() - 31, 145, 20)
                painter.drawText(badge_rect, Qt.AlignLeft | Qt.AlignVCenter, f"当前分类：{category}")

        # 文件状态与分类、AI 状态独立；窄卡片仍可看到改名 / 撤销标记。
        if card.width() < 520 and self.file_badge(item):
            painter.setFont(normal_font)
            painter.setPen(palette.color(QPalette.Link))
            painter.drawText(QRect(card.right()-90, card.bottom()-29, 76, 20),
                Qt.AlignRight | Qt.AlignVCenter, self.file_badge(item))

        # 状态角标：文件状态、人工修改、人工确认锁、AI已审彼此独立。
        if card.width() >= 520:
            status_bits = [self.file_badge(item)] if self.file_badge(item) else []
            if manual:
                if item is not None and item.has_manual_version:
                    status_bits.append("✎手动版本" if name_source == "manual" else "↔保留手动")
                else:
                    status_bits.append("✎人工修改")
            if manual_confirmed:
                status_bits.append("已确认")
            status_bits.append(ai_status)
            if status_bits:
                badge_rect = QRect(card.right() - 270, card.bottom() - 29, 256, 20)
                painter.setPen(palette.color(QPalette.Link))
                painter.drawText(badge_rect, Qt.AlignRight | Qt.AlignVCenter, " · ".join(status_bits))

            warning = index.data(CardListModel.WARNING_ROLE) or ""
            if warning:
                # 主卡片只给简短结论；完整原始警告仍保留在详情面板。
                item = index.data(CardListModel.ITEM_ROLE)
                analysis = (item.extra or {}).get("analysis") if item and isinstance(item.extra, dict) else {}
                page_level = analysis.get("page_diff_level") if isinstance(analysis, dict) else None
                first_warning = warning.split('；', 1)[0]
                if page_level in {"较大", "极大"}:
                    warning_text = "页数差较大"
                elif "artist/group" in first_warning or "作者" in first_warning or "社团" in first_warning:
                    warning_text = "作者/社团待确认"
                elif "Completed/Ongoing" in first_warning or "主体需要人工确认" in first_warning:
                    warning_text = "标题主体待确认"
                elif "Decensored" in first_warning and "uncensored" in first_warning:
                    warning_text = "去码标记待确认"
                else:
                    warning_text = "需要复核"

                painter.setPen(palette.color(QPalette.Link))
                # V0.2.13：警告从右侧“语言/团体/作者”粗体标签的左边界开始，
                # 不再把整组文字右对齐到一个过窄的固定框，避免“作者/社团待确…”截断。
                tag_block = self._tag_block_rect(option.rect)
                warn_left = tag_block.left() if not tag_block.isNull() else card.right() - 250
                warn_right = card.right() - 119  # 给右下角手动版本角标保留原有空间
                warn_rect = QRect(warn_left, card.bottom() - 29, max(40, warn_right - warn_left + 1), 20)
                fm = painter.fontMetrics()
                icon = "⚠"
                gap = max(3, fm.horizontalAdvance(" "))
                icon_w = fm.horizontalAdvance(icon)
                icon_rect = QRect(warn_rect.left(), warn_rect.top() - 2, icon_w, warn_rect.height())
                text_left = icon_rect.right() + 1 + gap
                text_rect = QRect(text_left, warn_rect.top(), max(1, warn_rect.right() - text_left + 1), warn_rect.height())
                painter.drawText(icon_rect, Qt.AlignLeft | Qt.AlignVCenter, icon)
                display_warning = fm.elidedText(warning_text, Qt.ElideRight, text_rect.width())
                painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter, display_warning)

        painter.restore()

    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setFrame(True)
        editor.setTextMargins(max(0, self.TEXT_INSET - 6), 0, max(0, self.TEXT_INSET - 6), 0)
        editor.setProperty("userActuallyEdited", False)
        return editor

    def setEditorData(self, editor, index):
        editor.setText(index.data(Qt.EditRole) or "")
        editor.setProperty("userActuallyEdited", False)
        editor.setCursorPosition(len(editor.text()))

        if not bool(editor.property("liveModelSyncConnected")):
            persistent = QPersistentModelIndex(index)
            model = index.model()

            def _sync_live_text(text, p=persistent, m=model):
                if p.isValid() and text.strip():
                    if hasattr(m, "update_manual_live"):
                        m.update_manual_live(p.row(), text)
                    else:
                        m.setData(QModelIndex(p), text, Qt.EditRole)

            def _mark_and_sync(text):
                editor.setProperty("userActuallyEdited", True)
                _sync_live_text(text)

            editor.textEdited.connect(_mark_and_sync)
            editor.setProperty("liveModelSyncConnected", True)

    def setModelData(self, editor, model, index):
        # 只点击文本框/移动光标/选择文字，不创建“手动版本”。
        if not bool(editor.property("userActuallyEdited")):
            return
        model.setData(index, editor.text(), Qt.EditRole)

    def updateEditorGeometry(self, editor, option, index):
        editor.setGeometry(self._second_line_rect(option.rect))
