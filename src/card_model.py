from __future__ import annotations

import json

from PySide6.QtCore import QAbstractListModel, QModelIndex, Qt, Signal

from .models import WorkItem


class CardListModel(QAbstractListModel):
    ITEM_ROLE = Qt.UserRole + 1
    ORIGINAL_ROLE = Qt.UserRole + 2
    SUGGESTED_ROLE = Qt.UserRole + 3
    WARNING_ROLE = Qt.UserRole + 4
    MANUAL_ROLE = Qt.UserRole + 5
    CHECKED_ROLE = Qt.UserRole + 6
    NAME_SOURCE_ROLE = Qt.UserRole + 7
    PROGRAM_SUGGESTION_ROLE = Qt.UserRole + 8
    MANUAL_NAME_ROLE = Qt.UserRole + 9
    TITLE_JPN_ROLE = Qt.UserRole + 10
    TITLE_ROLE = Qt.UserRole + 11
    FILE_NAME_ROLE = Qt.UserRole + 12
    LANGUAGE_TAGS_ROLE = Qt.UserRole + 13
    GROUP_TAGS_ROLE = Qt.UserRole + 14
    ARTIST_TAGS_ROLE = Qt.UserRole + 15
    CATEGORY_ROLE = Qt.UserRole + 16
    MANUAL_CONFIRMED_ROLE = Qt.UserRole + 17
    AI_REVIEWED_ROLE = Qt.UserRole + 18
    ATTRIBUTES_ROLE = Qt.UserRole + 19
    NEEDS_AI_ROLE = Qt.UserRole + 20

    itemEdited = Signal(object)
    manualStateChanged = Signal(object)

    def __init__(self, items: list[WorkItem] | None = None, parent=None):
        super().__init__(parent)
        self.items: list[WorkItem] = items or []
        self.confirm_confirmed_edit = None

    def rowCount(self, parent=QModelIndex()):
        return 0 if parent.isValid() else len(self.items)

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        """只负责读取模型数据。

        V0.2.1 曾误把编辑逻辑合并进这里，导致卡片绘制时访问不存在的
        ``value`` 变量，943 本完整库扫描后出现“空卡片”。V0.2.2 将
        读取和写入重新彻底分离。
        """
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return None
        item = self.items[index.row()]

        if role in (Qt.DisplayRole, self.ORIGINAL_ROLE):
            return item.original_name
        if role in (Qt.EditRole, self.SUGGESTED_ROLE):
            return item.suggested_name
        if role == self.ITEM_ROLE:
            return item
        if role == self.WARNING_ROLE:
            return item.warning
        if role == self.MANUAL_ROLE:
            return bool(item.manual_edited or item.has_manual_version)
        if role == self.CHECKED_ROLE:
            return item.checked
        if role == self.NAME_SOURCE_ROLE:
            return item.name_source
        if role == self.PROGRAM_SUGGESTION_ROLE:
            return item.program_suggested_name
        if role == self.MANUAL_NAME_ROLE:
            return item.manual_name
        if role == self.TITLE_JPN_ROLE:
            return item.record.title_jpn if item.record else ""
        if role == self.TITLE_ROLE:
            return item.record.title if item.record else ""
        if role == self.FILE_NAME_ROLE:
            return f"{item.original_name}{item.suffix}"
        if role == self.CATEGORY_ROLE:
            return item.category
        if role == self.MANUAL_CONFIRMED_ROLE:
            return item.manual_confirmed
        if role == self.AI_REVIEWED_ROLE:
            return item.ai_status
        if role == self.ATTRIBUTES_ROLE:
            return ", ".join(item.attributes or [])
        if role == self.NEEDS_AI_ROLE:
            return item.needs_ai
        if role in (self.LANGUAGE_TAGS_ROLE, self.GROUP_TAGS_ROLE, self.ARTIST_TAGS_ROLE):
            # 直接从当前 record.tags_raw 读取，避免把卡片标签写死在加载阶段。
            # 未来“刷新 E-H 元数据”只要更新 record 并触发 dataChanged，
            # 标题/英文标题与三行 Tag 就能一起刷新。
            try:
                tags = json.loads(item.record.tags_raw or "{}") if item.record else {}
                if not isinstance(tags, dict):
                    tags = {}
            except (TypeError, ValueError, json.JSONDecodeError):
                tags = {}
            key = {
                self.LANGUAGE_TAGS_ROLE: "language",
                self.GROUP_TAGS_ROLE: "group",
                self.ARTIST_TAGS_ROLE: "artist",
            }[role]
            values = tags.get(key, [])
            if isinstance(values, str):
                values = [values]
            if not isinstance(values, list):
                values = []
            return ", ".join(str(v).strip() for v in values if str(v).strip())
        if role == Qt.CheckStateRole:
            return Qt.Checked if item.checked else Qt.Unchecked
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole):
        if not index.isValid() or not (0 <= index.row() < len(self.items)):
            return False

        item = self.items[index.row()]

        if role == Qt.CheckStateRole:
            new_value = value == Qt.Checked or value == 2 or value is True
            if item.checked != new_value:
                item.checked = bool(new_value)
                self.dataChanged.emit(index, index, [Qt.CheckStateRole, self.CHECKED_ROLE])
            return True

        if role in (Qt.EditRole, self.SUGGESTED_ROLE):
            text = str(value).strip()
            if not text:
                return False
            if item.category == "已确认" and text != item.suggested_name:
                if not self.confirm_confirmed_edit or not self.confirm_confirmed_edit(item, text):
                    return False
                item.confirmed_name = text

            # 只有真正发生过文本修改时 delegate 才会调用 setData。
            # 如果用户最终把新产生的手动版本完整改回程序建议，则取消这份
            # 新手动版本；如果当前正在程序建议侧查看且已有历史手动版本，
            # 单纯查看/点击不会触碰那份手动版本。
            if text == item.program_suggested_name:
                if item.name_source == "manual":
                    item.manual_name = ""
                    # “人工修改”是历史标记：即使手动版本最终改回程序建议，也保留“曾人工编辑过”。
                    item.manual_edited = True
                    item.name_source = "program"
                    item.suggested_name = item.program_suggested_name
                    self.dataChanged.emit(
                        index,
                        index,
                        [
                            Qt.DisplayRole,
                            Qt.EditRole,
                            self.MANUAL_ROLE,
                            self.MANUAL_NAME_ROLE,
                            self.NAME_SOURCE_ROLE,
                        ],
                    )
                    self.itemEdited.emit(item)
                else:
                    # 编辑器结束编辑时补一次刷新；不会创建手动版本。
                    self.dataChanged.emit(
                        index,
                        index,
                        [Qt.DisplayRole, Qt.EditRole, self.NAME_SOURCE_ROLE],
                    )
                # live sync 已可能提前更新对象；正式结束编辑时仍必须通知主窗口保存。
                self.itemEdited.emit(item)
                return True

            changed = (
                item.suggested_name != text
                or item.manual_name != text
                or item.name_source != "manual"
            )
            if changed:
                item.manual_name = text
                item.manual_edited = True
                item.name_source = "manual"
                item.suggested_name = text
            # live sync 已可能提前把对象更新到最终值；无论 changed 与否都在编辑结束时正式提交。
            self.itemEdited.emit(item)

            # live sync 期间为了避免光标跳动没有发 dataChanged；编辑结束后
            # 无论数据是否已经同步，都在这里刷新一次卡片状态。
            self.dataChanged.emit(
                index,
                index,
                [
                    Qt.DisplayRole,
                    Qt.EditRole,
                    self.MANUAL_ROLE,
                    self.MANUAL_NAME_ROLE,
                    self.NAME_SOURCE_ROLE,
                ],
            )
            return True

        return False

    def update_manual_live(self, row: int, value: str) -> bool:
        """输入过程中只同步对象，不刷新模型/详情。

        V0.2.23 的 live sync 会经 itemEdited -> MainWindow.refresh_item 间接发出
        dataChanged，Qt 随后重置编辑器数据，造成每输入/删除一个字符光标跳到末尾。
        V0.2.24 改为编辑结束时再由 setModelData 发正式信号与自动保存。
        """
        if not (0 <= row < len(self.items)):
            return False
        text = str(value)
        if not text.strip():
            return True
        item = self.items[row]
        if item.category == "已确认":
            return True

        if text.strip() == item.program_suggested_name:
            if item.name_source == "manual":
                item.manual_name = ""
                item.manual_edited = True
                item.name_source = "program"
                item.suggested_name = item.program_suggested_name
                self.manualStateChanged.emit(item)
            return True

        state_changed = item.name_source != "manual" or not item.manual_name
        item.manual_name = text.strip()
        item.manual_edited = True
        item.name_source = "manual"
        item.suggested_name = text.strip()
        if state_changed:
            self.manualStateChanged.emit(item)
        # 关键：不发 dataChanged，不重置编辑器；只用轻量信号让右侧切换按钮即时亮起。
        return True

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.NoItemFlags
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        item = self.items[index.row()] if 0 <= index.row() < len(self.items) else None
        if item is not None:
            flags |= Qt.ItemIsEditable
        return flags

    def checked_rows(self) -> list[int]:
        return [i for i, item in enumerate(self.items) if item.checked]

    def set_checked(self, row: int, checked: bool):
        if 0 <= row < len(self.items):
            idx = self.index(row, 0)
            self.setData(idx, Qt.Checked if checked else Qt.Unchecked, Qt.CheckStateRole)

    def set_checked_rows(self, rows: set[int], checked: bool = True):
        for row in rows:
            self.set_checked(row, checked)

    def clear_checks(self):
        changed = []
        for row, item in enumerate(self.items):
            if item.checked:
                item.checked = False
                changed.append(row)
        if changed:
            self.dataChanged.emit(
                self.index(min(changed), 0),
                self.index(max(changed), 0),
                [Qt.CheckStateRole, self.CHECKED_ROLE],
            )

    def check_all(self):
        if not self.items:
            return
        for item in self.items:
            item.checked = True
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self.items) - 1, 0),
            [Qt.CheckStateRole, self.CHECKED_ROLE],
        )

    def invert_checks(self):
        if not self.items:
            return
        for item in self.items:
            item.checked = not item.checked
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self.items) - 1, 0),
            [Qt.CheckStateRole, self.CHECKED_ROLE],
        )

    def refresh_checks(self):
        """Repaint page checkboxes after full-scope selection changed."""
        if not self.items:
            return
        self.dataChanged.emit(
            self.index(0, 0),
            self.index(len(self.items) - 1, 0),
            [Qt.CheckStateRole, self.CHECKED_ROLE],
        )

    def set_items(self, items: list[WorkItem]):
        self.beginResetModel()
        self.items = items
        self.endResetModel()

    def take_rows(self, rows: list[int]) -> list[WorkItem]:
        row_set = set(rows)
        result = [self.items[r] for r in sorted(row_set) if 0 <= r < len(self.items)]
        if not result:
            return []
        keep = [item for i, item in enumerate(self.items) if i not in row_set]
        self.set_items(keep)
        return result

    def append_items(self, items: list[WorkItem]):
        if not items:
            return
        start = len(self.items)
        self.beginInsertRows(QModelIndex(), start, start + len(items) - 1)
        self.items.extend(items)
        self.endInsertRows()

    def refresh_item(self, item: WorkItem):
        for row, existing in enumerate(self.items):
            if existing is item:
                self._refresh_row(row)

    def _refresh_row(self, row: int):
        if not (0 <= row < len(self.items)):
            return
        idx = self.index(row, 0)
        self.dataChanged.emit(
            idx,
            idx,
            [
                Qt.DisplayRole,
                Qt.EditRole,
                self.MANUAL_ROLE,
                self.MANUAL_NAME_ROLE,
                self.NAME_SOURCE_ROLE,
                self.PROGRAM_SUGGESTION_ROLE,
                self.MANUAL_CONFIRMED_ROLE,
                self.AI_REVIEWED_ROLE,
                self.ATTRIBUTES_ROLE,
                self.NEEDS_AI_ROLE,
            ],
        )

    def switch_to_program(self, row: int) -> bool:
        if not (0 <= row < len(self.items)):
            return False
        item = self.items[row]
        if not item.program_suggested_name:
            return False
        if item.category == "已确认" and item.suggested_name != item.program_suggested_name:
            if not self.confirm_confirmed_edit or not self.confirm_confirmed_edit(item, item.program_suggested_name):
                return False
            item.confirmed_name = item.program_suggested_name
        item.name_source = "program"
        item.suggested_name = item.program_suggested_name
        self._refresh_row(row)
        self.itemEdited.emit(item)
        return True

    def switch_to_manual(self, row: int) -> bool:
        if not (0 <= row < len(self.items)):
            return False
        item = self.items[row]
        if not item.manual_name:
            return False
        if item.category == "已确认" and item.suggested_name != item.manual_name:
            if not self.confirm_confirmed_edit or not self.confirm_confirmed_edit(item, item.manual_name):
                return False
            item.confirmed_name = item.manual_name
        item.name_source = "manual"
        item.suggested_name = item.manual_name
        item.manual_edited = True
        self._refresh_row(row)
        self.itemEdited.emit(item)
        return True
