"""Two-tab preparation/history window for durable real file operations."""
from __future__ import annotations
import copy
import os
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QBrush
from PySide6.QtWidgets import (QDialog, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLabel, QComboBox, QCheckBox, QTabWidget, QTableWidgetItem, QTreeWidgetItem,
    QAbstractItemView, QHeaderView, QProgressBar, QApplication, QMessageBox,
    QSplitter, QListWidget, QListWidgetItem, QPlainTextEdit, QLineEdit, QFileDialog, QFrame)
from .ui_common import AlignedComboBox
from .models import CATEGORIES
from .review_ui import page_layout, match_tabs
from .file_review_ui import FileTable, FileHistory, CONFLICT_COLORS, GROUP_START_ROLE, UNDO_COLOR, undo_summary
from .file_tasks import counts, ROW_LABELS, TASK_LABELS, UNFINISHED, preflight
from .file_operations import action_label, file_labels


def cell(text):
    item = QTableWidgetItem(str(text))
    item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
    item.setToolTip(str(text) + '\n右键复制完整内容；双击可拖选部分文字复制。')
    return item


class FileDialog(QDialog):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.set_minimize_enabled(False)
        self.setWindowTitle('改名 / 移动')
        self.resize(1150, 760)
        self.setMinimumSize(650, 460)
        self.categories = ['已确认']
        self.rows = []
        self.action = 'rename'
        self._checked_destination = ''
        self.selected = set()
        self._selection_key = None
        self.task_id = ''
        self._filling = False
        self._detail_filling = False
        self._undo_selected = set()
        self._detail_selection_key = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(180)
        self._timer.timeout.connect(self.refresh)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        match_tabs(self.tabs)
        root.addWidget(self.tabs)
        self._build_preparation()
        self._build_history()
        self.tabs.currentChanged.connect(self._tab_changed)

    def _build_preparation(self):
        self.rename_page, self.move_page = QWidget(), QWidget()
        self.rename_layout, self.move_layout = QVBoxLayout(self.rename_page), QVBoxLayout(self.move_page)
        for holder in (self.rename_layout,self.move_layout): holder.setContentsMargins(0,0,0,0)
        self.tabs.addTab(self.rename_page,'改名')
        self.tabs.addTab(self.move_page,'移动')
        self.preparation = QWidget()
        self.rename_layout.addWidget(self.preparation)
        layout = page_layout(self.preparation)
        row = QHBoxLayout()
        row.addWidget(QLabel('选择范围'))
        self.scope = AlignedComboBox()
        for label, key in [('已勾选', 'checked'), ('当前搜索结果', 'search'), ('指定分类（已确认）', 'categories')]:
            self.scope.addItem(label, key)
        row.addWidget(self.scope, 1)
        self.category_btn = QPushButton('选择分类')
        self.category_btn.clicked.connect(self.choose_categories)
        row.addWidget(self.category_btn)
        self.exclude = QCheckBox('排除已改名')
        self.exclude.setChecked(True)
        row.addWidget(self.exclude)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.filter = AlignedComboBox()
        for label, key in [('全部', 'all'), ('可执行', 'safe'), ('冲突', 'blocked'), ('无需执行', 'noop')]:
            self.filter.addItem(label, key)
        row.addWidget(self.filter)
        row.addStretch()
        self.check_btn = QPushButton('重新检查')
        self.check_btn.clicked.connect(self.refresh)
        row.addWidget(self.check_btn)
        layout.addLayout(row)
        self.preview = FileTable(['', '当前名称', '最终名称', '状态', '原路径', '目标路径', '原因'],
            (45, 230, 230, 175, 310, 310, 260), check_column=0)
        self.preview.enable_select_all_header()
        self.preview.horizontalHeader().sectionClicked.connect(self._header_clicked)
        self.preview.itemChanged.connect(self._preview_check_changed)
        layout.addWidget(self.preview, 1)
        divider=QFrame();divider.setFrameShape(QFrame.HLine);layout.addWidget(divider)
        bottom=QHBoxLayout()
        self.destination_label=QLabel('目标目录')
        self.destination_edit=QLineEdit(self.owner.dest_edit.text())
        self.destination_edit.setPlaceholderText('选择本地目标目录')
        self.destination_edit.setMinimumWidth(110)
        self.destination_btn=QPushButton('选择目录')
        self.destination_btn.clicked.connect(self.choose_destination)
        self.destination_edit.textChanged.connect(self._destination_changed)
        bottom.addWidget(self.destination_label);bottom.addWidget(self.destination_edit,1);bottom.addWidget(self.destination_btn)
        self.start_btn = QPushButton('开始改名 0 本')
        self.start_btn.clicked.connect(self.start)
        bottom.addWidget(self.start_btn)
        layout.addLayout(bottom)
        for widget in (self.destination_label,self.destination_edit,self.destination_btn): widget.hide()
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.scope.activated.connect(lambda *_: self.refresh())
        self.exclude.toggled.connect(self._scope_changed)
        self.filter.currentIndexChanged.connect(self._fill_preview)

    def _build_history(self):
        page = QWidget()
        layout = page_layout(page)
        self.tabs.addTab(page, '任务与记录')
        self.current_label = QLabel('尚无文件任务')
        self.current_label.setWordWrap(True)
        self.current_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.current_label)
        row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setFormat('已处理 %v / %m 本')
        row.addWidget(self.progress, 1)
        self.stop_btn = QPushButton('暂停')
        self.stop_btn.clicked.connect(self.owner.stop_file_task)
        row.addWidget(self.stop_btn)
        self.resume_btn = QPushButton('继续')
        self.resume_btn.clicked.connect(self.owner.continue_file_queue)
        row.addWidget(self.resume_btn)
        layout.addLayout(row)
        self.item_progress_label=QLabel('')
        self.item_progress_label.setWordWrap(True)
        self.item_progress=QProgressBar()
        layout.addWidget(self.item_progress_label)
        layout.addWidget(self.item_progress)
        queue_row=QHBoxLayout()
        self.queue_label=QLabel()
        queue_row.addWidget(self.queue_label,1)
        self.queue_cancel_btn=QPushButton('取消所选排队任务')
        self.queue_cancel_btn.clicked.connect(lambda: self.owner.cancel_queued_file_task(self.task_id))
        queue_row.addWidget(self.queue_cancel_btn)
        layout.addLayout(queue_row)
        self.splitter = QSplitter(Qt.Vertical)
        self.history = FileHistory()
        self.history.itemSelectionChanged.connect(self._select_task)
        self.splitter.addWidget(self.history)
        detail = QWidget()
        detail_layout = QVBoxLayout(detail)
        detail_layout.setContentsMargins(0, 2, 0, 0)
        row = QHBoxLayout()
        self.detail_filter = AlignedComboBox()
        for text, key in [('全部', 'all'), ('成功', 'success'), ('失败', 'failed'), ('未执行', 'unexecuted'), ('执行前阻断', 'blocked'), ('待确认', 'uncertain')]:
            self.detail_filter.addItem(text, key)
        self.detail_filter.currentIndexChanged.connect(self._fill_detail)
        row.addWidget(self.detail_filter)
        self.readonly_label = QLabel()
        row.addWidget(self.readonly_label, 1)
        self.log_btn = QPushButton('查看记录')
        self.log_btn.clicked.connect(self.view_record)
        row.addWidget(self.log_btn)
        self.recheck_btn = QPushButton('重新核对')
        self.recheck_btn.clicked.connect(self.recheck)
        row.addWidget(self.recheck_btn)
        detail_layout.addLayout(row)
        self.detail = FileTable(['', '漫画', '最终名称', '状态', '原路径', '目标路径', '原因'],
            (45, 230, 230, 175, 310, 310, 260), check_column=0)
        self.detail.enable_select_all_header()
        self.detail.horizontalHeader().sectionClicked.connect(self._detail_header_clicked)
        self.detail.itemChanged.connect(self._detail_check_changed)
        detail_layout.addWidget(self.detail)
        self.splitter.addWidget(detail)
        self.splitter.setSizes([180, 340])
        layout.addWidget(self.splitter, 1)
        row = QHBoxLayout()
        self.legacy_resume_btn = QPushButton('继续此旧任务')
        self.legacy_resume_btn.clicked.connect(lambda: self.owner.resume_legacy_file_task(self.task_id))
        self.legacy_resume_btn.setToolTip('继续下方选中的旧版未完成任务；须先完成当前任务和等待队列，有异常时先重试或重新核对。')
        self.legacy_resume_btn.hide()
        self.retry_btn = QPushButton('重试失败项')
        self.retry_btn.clicked.connect(lambda: self.owner.resume_file_task(self.task_id, True))
        self.undo_btn = QPushButton('撤销整个任务')
        self.undo_btn.setToolTip('改名任务恢复原名并保留当前位置；移动任务移回原处并保留当前名称。每项重新做安全检查。')
        self.undo_btn.clicked.connect(lambda: self.owner.undo_file_task(self.task_id))
        self.undo_selected_btn = QPushButton('撤销勾选 0 本')
        self.undo_selected_btn.setToolTip('只撤销下方明细中勾选的漫画；仅选中整行不算勾选。')
        self.undo_selected_btn.clicked.connect(lambda: self.owner.undo_file_task(self.task_id, set(self._undo_selected)))
        self.clean_btn = QPushButton('清理已完成记录')
        self.clean_btn.clicked.connect(self.clean)
        for widget in (self.legacy_resume_btn, self.retry_btn, self.undo_selected_btn, self.undo_btn, self.clean_btn):
            row.addWidget(widget)
        layout.addLayout(row)

    def set_minimize_enabled(self, enabled=False):
        """Opt-in interface reserved for a future independent-window policy."""
        flags = self.windowFlags() & ~(Qt.WindowMinimizeButtonHint | Qt.WindowContextHelpButtonHint)
        flags |= Qt.WindowMaximizeButtonHint | Qt.WindowCloseButtonHint
        if enabled:
            flags |= Qt.WindowMinimizeButtonHint
        self.setWindowFlags(flags)

    def _tab_changed(self,index):
        if index in (0,1):
            self.action = 'rename' if index==0 else 'move'
            holder = self.rename_layout if index==0 else self.move_layout
            holder.addWidget(self.preparation)
            self.preparation.show()
            for widget in (self.destination_label,self.destination_edit,self.destination_btn): widget.setVisible(index==1)
            self.exclude.setVisible(index==0)
            self._selection_key=None
        self.refresh()

    def _destination_changed(self,text):
        self.owner.dest_edit.setText(text)
        self._checked_destination=''
        self.start_btn.setEnabled(False)
        self._selection_key=None
        self._timer.start()

    def choose_destination(self):
        path=QFileDialog.getExistingDirectory(self,'选择本地目标目录',self.destination_edit.text().strip() or self.owner.work_edit.text())
        if path:
            self.destination_edit.setText(path)
            self.refresh()

    def _scope_changed(self, *_):
        self._selection_key = None
        self.refresh()

    def choose_categories(self):
        dialog = QDialog(self)
        dialog.setWindowTitle('指定分类')
        layout = QVBoxLayout(dialog)
        choices = QListWidget()
        for category in CATEGORIES:
            entry = QListWidgetItem(category)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable)
            entry.setCheckState(Qt.Checked if category in self.categories else Qt.Unchecked)
            choices.addItem(entry)
        layout.addWidget(choices)
        ok = QPushButton('确定')
        ok.clicked.connect(dialog.accept)
        layout.addWidget(ok)
        if dialog.exec() == QDialog.Accepted:
            self.categories = [choices.item(i).text() for i in range(choices.count()) if choices.item(i).checkState() == Qt.Checked]
            self.scope.setItemText(self.scope.findData('categories'), '指定分类（' + '、'.join(self.categories) + '）')
            self._scope_changed()

    def schedule_refresh(self):
        if self.isVisible():
            self._timer.start()

    def refresh(self):
        self._timer.stop()
        self.category_btn.setEnabled(self.scope.currentData() == 'categories')
        try:
            rows = self.owner.file_preview(self.scope.currentData(), self.categories, self.exclude.isChecked(),self.action,self.destination_edit.text().strip())
        except (OSError, ValueError) as exc:
            rows = []
            self.start_btn.setToolTip(str(exc))
        self._checked_destination=self.destination_edit.text().strip()
        key = (self.action, self._checked_destination, self.scope.currentData(), tuple(self.categories), self.exclude.isChecked(),
               tuple((r['local_id'], r['target_path'], r['blocking_conflict'], r['executable']) for r in rows))
        safe = {r['local_id'] for r in rows if r['executable']}
        if self._selection_key != key:
            # Preserve explicit exclusions when only a disk/status refresh changed the pool.
            old_ids = {r['local_id'] for r in self.rows}
            self.selected = safe if self._selection_key is None else (self.selected & safe) | (safe - old_ids)
        self._selection_key, self.rows = key, rows
        self.filter.blockSignals(True)
        blocked = sum(r['blocking_conflict'] for r in rows)
        for i, count in enumerate((len(rows), len(safe), blocked, len(rows) - len(safe) - blocked)):
            self.filter.setItemText(i, ['全部', '可执行', '冲突', '无需移动' if self.action=='move' else '无需执行'][i] + f'（{count}）')
        self.filter.blockSignals(False)
        self._fill_preview()
        self._fill_history()

    def _fill_preview(self, *_):
        mode = self.filter.currentData()
        rows = [r for r in self.rows if mode == 'all' or (mode == 'safe' and r['executable'])
                or (mode == 'blocked' and r['blocking_conflict'])
                or (mode == 'noop' and not r['blocking_conflict'] and not r['executable'])]
        rows.sort(key=lambda r: (0, int(r['conflict_group'][1:])) if r.get('conflict_group') else (1, 0))
        self._filling = True
        self.preview.setUpdatesEnabled(False)
        self.preview.setRowCount(len(rows))
        for number, row in enumerate(rows):
            check = cell('')
            check.setData(Qt.UserRole, row['local_id'])
            if row['executable']:
                check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if row['local_id'] in self.selected else Qt.Unchecked)
            self.preview.setItem(number, 0, check)
            status = row['status'] + (' ' + row['conflict_group'] if row.get('conflict_group') else '')
            if row.get('file_labels'): status += ' · ' + ' · '.join(row['file_labels'])
            for column, text in enumerate((row['current_name'], row['final_name'], status,
                    row['item'].original_path, row['target_path'], row['message']), 1):
                self.preview.setItem(number, column, cell(text))
            if row.get('conflict_group'):
                for column in range(self.preview.columnCount()):
                    item = self.preview.item(number, column)
                    item.setBackground(QBrush(QColor(CONFLICT_COLORS[row['conflict_color']])))
                    item.setData(GROUP_START_ROLE, number == 0 or rows[number-1].get('conflict_group') != row['conflict_group'])
        self.preview.setUpdatesEnabled(True)
        self._filling = False
        self._update_start()

    def _preview_check_changed(self, item):
        if self._filling or item.column() != 0:
            return
        identity = item.data(Qt.UserRole)
        if item.checkState() == Qt.Checked:
            self.selected.add(identity)
        else:
            self.selected.discard(identity)
        self._update_start()

    def _header_clicked(self, column):
        if column != 0:
            return
        eligible = [self.preview.item(r, 0) for r in range(self.preview.rowCount())
                    if self.preview.item(r, 0).flags() & Qt.ItemIsUserCheckable]
        clear = bool(eligible) and all(i.checkState() == Qt.Checked for i in eligible)
        for item in eligible:
            item.setCheckState(Qt.Unchecked if clear else Qt.Checked)

    def _update_start(self):
        n = sum(r['local_id'] in self.selected and r['executable'] for r in self.rows)
        queue=self.owner.file_queue()
        queued=self.action=='move' and (self.owner.file_busy() or queue.control_task() is not None)
        label='加入队列（已暂停）' if queued and queue.paused and not self.owner.file_busy() else ('加入队列' if queued else ('开始移动' if self.action=='move' else '开始改名'))
        self.start_btn.setText(f"{label} {n} 本")
        self.start_btn.setEnabled(n>0 and (self.action=='move' or (not self.owner.file_busy() and not self.owner.file_queue().waiting())) and bool(self.owner.all_items) and (self.action!='move' or self._checked_destination==self.destination_edit.text().strip()))

    def start(self):
        if not self.start_btn.isEnabled(): return
        plans=[copy.deepcopy(r['plan']) for r in self.rows if r['local_id'] in self.selected and r['executable']]
        if self.action=='move':
            destination=self.destination_edit.text().strip()
            if QMessageBox.question(self,'确认移动',f'移动 {len(plans)} 本，保留当前名称。\n目标目录：{destination}\n按提交顺序执行，是否提交？',QMessageBox.Yes|QMessageBox.No,QMessageBox.No)!=QMessageBox.Yes: return
        self.owner.start_file_task(plans,self.action)

    def show_task(self, task_id):
        self.task_id = task_id
        self.tabs.setCurrentIndex(2)
        self._fill_history()
        self.owner.file_notice_btn.hide()

    def _fill_history(self):
        store = self.owner.file_store()
        tasks = store.list()
        expanded = {node.data(0, Qt.UserRole) for node in getattr(self, '_history_nodes', {}).values() if node.isExpanded()}
        self.history.blockSignals(True)
        self.history.clear()
        self._history_nodes = {}
        by_id = {task['task_id']: task for task in tasks}
        positions={task['task_id']:n+1 for n,task in enumerate(self.owner.file_queue().waiting())}
        self._undo_children = {}
        for task in tasks:
            parent = by_id.get(task.get('parent_task_id'))
            if task['action'] in {'undo','undo_move'} and parent and parent['action'] in {'rename','move'} and parent['library_id'] == task['library_id']:
                self._undo_children.setdefault(parent['task_id'], []).append(task)

        def add(task, parent_node=None):
            c = counts(task)
            current = bool(self.owner._loaded_source and task['library_id'] == self.owner.library_id)
            kind = action_label(task)
            if task['action'] in {'undo','undo_move'} and not parent_node:
                kind += '（原记录无法关联）' if task.get('parent_task_id') in by_id else '（原记录已清理）'
            summary = undo_summary(task,tasks)[0] if task['action'] in {'rename','move'} else '-'
            values = (task['created_at'][:19].replace('T', ' '), '当前库' if current else '历史库', kind,
                len(task['rows']), c['success'], c['failed'], c['pending'] + c['unexecuted'],
                (f"排队等待 · 第 {positions[task['task_id']]} 位" if task['state']=='queued' else TASK_LABELS.get(task['state'], task['state'])), summary)
            node = QTreeWidgetItem([str(value) for value in values])
            node.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if task['action'] in {'undo','undo_move'}:
                for column in range(len(values)):
                    node.setBackground(column, QBrush(QColor(UNDO_COLOR)))
            node.setData(0, Qt.UserRole, task['task_id'])
            node.setToolTip(0, task['task_id'] + '\n漫画库：' + task['library_id'] + '\n' + task['work_directory'] +
                ('\n原操作任务：' + task['parent_task_id'] if task.get('parent_task_id') else ''))
            if parent_node:
                parent_node.addChild(node)
            else:
                self.history.addTopLevelItem(node)
            self._history_nodes[task['task_id']] = node
            node.setExpanded(task['task_id'] in expanded)
            for child in self._undo_children.get(task['task_id'], []):
                add(child, node)
            return node

        attached = {child['task_id'] for children in self._undo_children.values() for child in children}
        for task in tasks:
            if task['task_id'] not in attached:
                add(task)
        if self.task_id not in self._history_nodes:
            self.task_id = tasks[0]['task_id'] if tasks else ''
        selected = self._history_nodes.get(self.task_id)
        if selected:
            if selected.parent():
                selected.parent().setExpanded(True)
            self.history.setCurrentItem(selected)
            self.history.scrollToItem(selected)
        self.history.blockSignals(False)
        queue=self.owner.file_queue()
        controlled=queue.control_task()
        recent = self.owner._file_runner.task if self.owner._file_runner else (controlled or (tasks[0] if tasks else None))
        if recent:
            c = counts(recent)
            self.current_label.setText(('当前任务' if self.owner.file_busy() else '最近任务') +
                f"：{action_label(recent)} ｜ {TASK_LABELS.get(recent['state'],recent['state'])} ｜ 总数 {len(recent['rows'])} ｜ 成功 {c['success']} ｜ 失败 {c['failed']} ｜ 阻断 {c['blocked']} ｜ 未执行 {c['pending']+c['unexecuted']} ｜ 待确认 {c['uncertain']}")
            self.progress.setRange(0, len(recent['rows']))
            self.progress.setValue(c['success'] + c['failed'] + c['blocked'])
        else:
            self.current_label.setText('尚无文件任务' + ('；有任务记录读取异常：' + '; '.join(store.errors) if store.errors else ''))
            self.progress.setRange(0, 1); self.progress.setValue(0)
        waiting=queue.waiting()
        runner=self.owner._file_runner
        pausing=bool(runner and runner.stop.is_set())
        suffix=''
        if pausing:
            suffix=' · 正在暂停并保存，后续任务等待'
        elif queue.paused and controlled:
            problems=counts(controlled)
            suffix=' · 队列已暂停：有异常，请处理后继续' if any(problems[k] for k in ('failed','blocked','uncertain')) else ' · 队列已暂停，点击上方继续'
        elif controlled:
            suffix=' · 按顺序执行'
        self.queue_label.setText(f'等待队列：{len(waiting)} 个任务'+suffix)
        self.stop_btn.setText('正在暂停…' if pausing else '暂停')
        self.stop_btn.setEnabled(bool(runner and not pausing))
        current_control=bool(controlled and self.owner._loaded_source and controlled['library_id']==self.owner.library_id)
        self.resume_btn.setEnabled(not self.owner.file_busy() and current_control)
        self.resume_btn.setToolTip('继续当前暂停的任务，完成后自动执行等待队列；不随下方选中行改变。')
        self.update_progress()
        self._fill_detail()

    def update_progress(self):
        value=getattr(self.owner,'_file_progress',{})
        visible=bool(value and self.owner.file_busy())
        self.item_progress_label.setVisible(visible)
        self.item_progress.setVisible(visible)
        if not visible: return
        phases={'copying':'复制','verifying':'校验','cleanup':'清理源文件','done':'完成','moving':'同盘移动'}
        done,total=value['bytes_done'],value['bytes_total']
        self.item_progress_label.setText(f"当前漫画：{value['name']} · {phases.get(value['phase'],value['phase'])}" + (f" · {done/1048576:.1f} / {total/1048576:.1f} MB" if total else ''))
        if total:
            self.item_progress.setRange(0,1000);self.item_progress.setValue(min(1000,done*1000//total))
        else:
            self.item_progress.setRange(0,1);self.item_progress.setValue(value['phase']=='done')

    def _select_task(self):
        item = self.history.currentItem()
        self.task_id = item.data(0, Qt.UserRole) if item else ''
        self._fill_detail()

    def _fill_detail(self, *_):
        task = self.owner.file_store().tasks.get(self.task_id)
        current = bool(task and self.owner._loaded_source and task['library_id'] == self.owner.library_id)
        mode = self.detail_filter.currentData()
        key = (self.task_id, mode)
        if key != self._detail_selection_key:
            self._undo_selected.clear()
            self._detail_selection_key = key
        children = getattr(self, '_undo_children', {}).get(self.task_id, [])
        undone = self.owner.file_store().undone_rows(self.task_id)
        plans, undo_problem = {}, ''
        if current and task['action'] in {'rename','move'}:
            try:
                plans = {p['parent_row']: p for p in self.owner.file_undo_plans(self.task_id)}
            except ValueError as exc:
                undo_problem = str(exc)
        directories = {}
        ai_ids = self.owner.active_ai_ids() if plans else set()
        problems = {identity: preflight(plan, ai_ids, directories) for identity, plan in plans.items()}
        self._detail_eligible = {identity for identity in plans if not problems[identity]} if not self.owner.file_busy() else set()
        self._undo_selected.intersection_update(self._detail_eligible)
        rows = task['rows'] if task else []
        rows = [r for r in rows if mode == 'all' or r['state'] == mode or (mode == 'unexecuted' and r['state'] == 'pending')]
        self._detail_filling = True
        self.detail.setUpdatesEnabled(False)
        self.detail.setRowCount(len(rows))
        for number, row in enumerate(rows):
            check = cell('')
            check.setData(Qt.UserRole, row['local_id'])
            if row['local_id'] in self._detail_eligible:
                check.setFlags(check.flags() | Qt.ItemIsUserCheckable)
            check.setCheckState(Qt.Checked if row['local_id'] in self._undo_selected else Qt.Unchecked)
            check.setToolTip('勾选后点击下方“恢复原名”或“移回原处”。' if row['local_id'] in self._detail_eligible else
                problems.get(row['local_id']) or undo_problem or '该项目前不可勾选撤销。')
            self.detail.setItem(number, 0, check)
            status = ROW_LABELS.get(row['state'], row['state'])
            if row['state'] == 'success':
                if task['action'] in {'undo','undo_move'} or row['local_id'] in undone:
                    status='已撤销移动' if task['action'] in {'move','undo_move'} else '已撤销改名'
                else:
                    status='已移动' if task['action']=='move' else '已改名'
                    plan=plans.get(row['local_id'])
                    if plan and plan.get('needs_move'): status += ' · 已移动'
            transfer=next((s['transfer'] for s in row.get('steps',[]) if s.get('transfer',{}).get('phase')=='cleanup_pending'),row.get('transfer',{}))
            if transfer.get('phase')=='cleanup_pending' and row['state']!='success': status='源清理待完成'
            reason = problems.get(row['local_id']) or row.get('reason') or '-'
            if row.get('steps'):
                reason = '；'.join(s['label']+'：'+ROW_LABELS[s['state']] for s in row['steps']) + ('；'+reason if reason!='-' else '')
            for column, text in enumerate((row['current_name'], row['final_name'], status,
                                           row['source_path'], row['target_path'], reason), 1):
                self.detail.setItem(number, column, cell(text))
        self.detail.setUpdatesEnabled(True)
        self._detail_filling = False
        c = counts(task or {})
        available = current and not self.owner.file_busy() and task['state'] not in {'queued','cancelled'}
        queue = self.owner.file_queue()
        legacy_remaining = bool(current and task['state'] not in {'completed', 'queued', 'cancelled'}
                                and task['task_id'] != queue.active_id and (c['pending'] or c['unexecuted']))
        self.legacy_resume_btn.setVisible(legacy_remaining)
        self.legacy_resume_btn.setEnabled(bool(legacy_remaining and available and queue.control_task() is None
                                              and not self.owner.file_store().errors
                                              and not any(c[k] for k in ('failed','blocked','uncertain'))))
        self.queue_cancel_btn.setEnabled(bool(current and task['state']=='queued' and self.owner.file_queue().active_id!=task['task_id']))
        self.readonly_label.setText(('当前库' + (' · ' + undo_problem if undo_problem else '')) if current else '历史库 · 仅查看；请先加载原漫画库')
        self.readonly_label.setWordWrap(True)
        self.retry_btn.setEnabled(available and not c['uncertain'] and bool(c['failed']))
        remaining = undo_summary(task,self.owner.file_store().list())[1] if task else 0
        self.undo_btn.setEnabled(bool(available and task and task['action'] in {'rename','move'} and remaining and not undo_problem and not self.owner.file_queue().waiting()))
        self.undo_btn.setText('全部移回原处' if task and task['action']=='move' else '全部恢复原名')
        self._update_undo_selected()
        self.recheck_btn.setEnabled(available)
        self.log_btn.setEnabled(bool(task))
        self.clean_btn.setEnabled(bool(task and not self.owner.file_busy() and all(r['state'] == 'success' for r in task['rows'])))

    def _detail_check_changed(self, item):
        if self._detail_filling or item.column() != 0:
            return
        identity = item.data(Qt.UserRole)
        if identity in self._detail_eligible and item.checkState() == Qt.Checked:
            self._undo_selected.add(identity)
        else:
            self._undo_selected.discard(identity)
        self._update_undo_selected()

    def _update_undo_selected(self):
        count = len(self._undo_selected & getattr(self, '_detail_eligible', set()))
        task = self.owner.file_store().tasks.get(self.task_id)
        label = '移回原处' if task and task['action']=='move' else '恢复原名'
        self.undo_selected_btn.setText(f'{label}（勾选 {count} 本）')
        self.undo_selected_btn.setEnabled(count > 0 and not self.owner.file_busy() and not self.owner.file_queue().waiting())

    def _detail_header_clicked(self, column):
        if column != 0:
            return
        eligible = [self.detail.item(r, 0) for r in range(self.detail.rowCount())
                    if self.detail.item(r, 0).flags() & Qt.ItemIsUserCheckable]
        clear = bool(eligible) and all(i.checkState() == Qt.Checked for i in eligible)
        for item in eligible:
            item.setCheckState(Qt.Unchecked if clear else Qt.Checked)

    def recheck(self):
        try:
            self.owner.file_store().recover(self.owner.library_id, recheck_blocked=True)
            self.owner.file_store().reconcile_items(self.owner.all_items, self.owner.library_id, self.owner.session_id, True)
            self.owner._refresh_after_file_change()
            self.refresh()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '重新核对未完成', str(exc))

    def clean(self):
        task = self.owner.file_store().tasks.get(self.task_id)
        if not task:
            return
        if QMessageBox.question(self, '清理已完成记录',
            '清理后，该任务不能再从界面发起撤销。普通运行日志仍保留。是否清理选中的已完成任务？',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        try:
            self.owner.file_store().clean(self.task_id)
            self.task_id = ''
            self.refresh()
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '记录未清理', str(exc))

    def view_record(self):
        task = self.owner.file_store().tasks.get(self.task_id)
        if not task:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('文件任务完整记录')
        dialog.resize(900, 600)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit()
        text.setReadOnly(True)
        lines = [f"任务：{task['task_id']}", f"漫画库：{task['library_id']}",
                 f"时间：{task['created_at']}", f"工作目录：{task['work_directory']}",
                 f"类型：{action_label(task)}",
                 f"状态：{TASK_LABELS.get(task['state'],task['state'])}"]
        if task.get('persistence_error'):
            lines.append('保存异常：' + task['persistence_error'])
        for row in task['rows']:
            lines += ['', '漫画：' + row['current_name'], '最终名称：' + row['final_name'],
                      '原路径：' + row['source_path'], '目标路径：' + row['target_path'],
                      '当前状态：' + ROW_LABELS[row['state']], '原因：' + (row.get('reason') or '-'),
                      f"累计尝试：{row.get('attempts',0)} 次"]
            for step in row.get('steps',[]):
                lines += ['  步骤：'+step['label']+' · '+ROW_LABELS[step['state']], '  路径：'+step['source_path']+' → '+step['target_path'], '  关联原任务：'+step['reverses_task_id']]
            for event in row.get('history', []):
                lines.append(f"  {event['at']}　{ROW_LABELS.get(event['state'],event['state'])}　{event.get('reason','')}")
        text.setPlainText('\n'.join(lines))
        layout.addWidget(text)
        close = QPushButton('关闭'); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec()

    def showEvent(self, event):
        super().showEvent(event)
        if self.isMaximized() or self.isMinimized():
            return
        screen = self.owner.screen()
        if screen is not None:
            area = screen.availableGeometry().adjusted(15, 15, -15, -15)
            self.resize(min(self.width(), area.width()), min(self.height(), area.height()))
            frame = self.frameGeometry()
            self.move(max(area.left(), min(frame.x(), area.right()-frame.width()+1)),
                      max(area.top(), min(frame.y(), area.bottom()-frame.height()+1)))

    def closeEvent(self, event):
        # This only hides the modeless window; the main-window task owns its runner.
        self._timer.stop()
        event.accept()
