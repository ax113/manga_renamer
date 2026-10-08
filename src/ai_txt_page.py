"""A compact TXT page and task actions within the existing AI dialog."""
from __future__ import annotations

import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics
from .ui_common import AlignedComboBox
from .review_ui import ScopeComboBox, page_layout, section_line, address_field, set_address, result_paths
from .ai_directory_import import scan_results
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QListWidget, QListWidgetItem, QPlainTextEdit, QPushButton, QSpinBox, QVBoxLayout,
    QWidget, QCheckBox, QMessageBox, QSizePolicy,QMenu,QApplication)

from .ai_supplements import enabled_supplements
from .ai_txt import split_sizes, ROW_LABELS
from .ai_txt_store import is_txt
from .models import CATEGORIES
from .run_log import _redact

TASK_LABELS = {"waiting": "等待结果", "export_failed": "部分待导出", "completed": "已结束"}


class ResultDrop(QLabel):
    def __init__(self, owner):
        super().__init__("把一个或多个 RESULT TXT 拖到这里")
        self.owner = owner
        self.setAcceptDrops(True)
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(55)
        self.setStyleSheet("QLabel { border: 1px dashed #999; padding: 8px; }")

    def dragEnterEvent(self, event):
        if result_paths(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        paths = result_paths(event.mimeData())
        if paths:
            event.acceptProposedAction()
            self.owner.import_txt_files(paths)


class TxtPage(QWidget):
    directoryRead = Signal(object)
    directoryProgress = Signal(int, int)

    def __init__(self, owner):
        super().__init__()
        self.owner = owner
        self.categories = []
        self.directory_busy = False
        self.directory_status = ''
        self.directoryRead.connect(self._directory_finished)
        self.directoryProgress.connect(self._directory_progress)
        layout = page_layout(self)
        self.scope_title = QLabel('选择范围')
        layout.addWidget(self.scope_title)
        self.scope = ScopeComboBox()
        for label, key in (("已勾选漫画", "checked"), ("指定分类", "categories"),
                           ("全部漫画", "all"), ("当前搜索结果", "search")):
            self.scope.addItem(label, key)
        scope_row = QHBoxLayout(); scope_row.addWidget(self.scope, 1)
        self.category_btn = QPushButton("选择分类")
        self.category_btn.clicked.connect(self.choose_categories)
        scope_row.addWidget(self.category_btn); layout.addLayout(scope_row)
        self.range_label = QLabel()
        layout.addWidget(self.range_label)
        row = QHBoxLayout()
        self.mode = AlignedComboBox()
        self.mode.addItem("每份漫画本数", "books")
        self.mode.addItem("拆成文件份数", "parts")
        self.value = QSpinBox()
        self.value.setRange(1, 1000000)
        saved = owner.settings_data.get("txt_split", {})
        saved = saved if isinstance(saved, dict) else {}
        self.values = {}
        for key, default in (("books", 10), ("parts", 1)):
            try:
                self.values[key] = max(1, min(1000000, int(saved.get(key, default))))
            except (ValueError, TypeError):
                self.values[key] = default
        mode = saved.get("mode", "books")
        self.mode.setCurrentIndex(max(0, self.mode.findData(mode)))
        self.value.setValue(max(1, self.values.get(mode, 10)))
        row.addWidget(self.mode)
        row.addWidget(self.value)
        layout.addLayout(row)
        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        export_directory_row = QHBoxLayout()
        export_directory_row.addWidget(QLabel('导出目录'))
        self.export_directory = address_field(); export_directory_row.addWidget(self.export_directory,1)
        self.export_directory_change = QPushButton('修改导出文件夹')
        self.export_directory_change.clicked.connect(self.choose_export_directory)
        export_directory_row.addWidget(self.export_directory_change)
        layout.addLayout(export_directory_row)
        export_row = QHBoxLayout()
        self.export_btn = QPushButton("一键导出 INPUT TXT")
        self.export_btn.clicked.connect(self.export)
        export_row.addWidget(self.export_btn)
        folder = QPushButton("打开最近导出文件夹")
        folder.clicked.connect(self.open_folder)
        export_row.addWidget(folder)
        layout.addLayout(export_row)
        self.export_note = QLabel()
        self.export_note.setWordWrap(True)
        layout.addWidget(self.export_note)
        self.materials_btn = QPushButton('规则与提示词')
        materials_menu = QMenu(self.materials_btn)
        materials_menu.addAction('导出规则资料包',self.export_materials)
        materials_menu.addAction('复制项目指令',self.copy_project_instructions)
        materials_menu.addAction('复制送审提示词',self.copy_submission)
        materials_menu.addAction('使用说明',self.show_material_help)
        self.materials_btn.setMenu(materials_menu); layout.addWidget(self.materials_btn)
        layout.addWidget(section_line())
        layout.addWidget(QLabel("导入 RESULT，工具自动核验并应用有效结果"))
        self.directory_target = QLabel(); self.directory_target.setMinimumWidth(0)
        layout.addWidget(self.directory_target)
        directory_row = QHBoxLayout()
        directory_row.addWidget(QLabel('结果目录'))
        self.directory_label = address_field(); directory_row.addWidget(self.directory_label,1)
        self.directory_change = QPushButton('更改目录'); self.directory_change.clicked.connect(self.choose_directory); directory_row.addWidget(self.directory_change)
        self.directory_button = QPushButton('从结果目录导入'); self.directory_button.clicked.connect(lambda:self.import_directory(self.recent_task_id()))
        directory_row.addWidget(self.directory_button); layout.addLayout(directory_row)
        self.drop_area = ResultDrop(owner)
        layout.addWidget(self.drop_area)
        imports = QHBoxLayout()
        select = QPushButton("导入TXT结果")
        select.clicked.connect(self.choose_results)
        imports.addWidget(select)
        retry = QPushButton("重试待核对结果")
        retry.clicked.connect(owner.retry_deferred_txt)
        imports.addWidget(retry)
        layout.addLayout(imports)
        from .ai_archive_ui import ArchiveControls
        self.archive_controls = ArchiveControls(owner); layout.addWidget(self.archive_controls)
        self.summary = QLabel("可一次选择多份、乱序或不同任务的 RESULT。")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.anomaly_toggle = QPushButton("查看导入异常")
        layout.addWidget(self.anomaly_toggle)
        self.anomalies = QPlainTextEdit()
        self.anomalies.setReadOnly(True)
        self.anomalies.setMinimumHeight(75)
        self.anomalies.setVisible(False)
        self.anomaly_toggle.clicked.connect(self._toggle_anomalies)
        layout.addWidget(self.anomalies)
        layout.addStretch()
        # Text must wrap or elide within the page, never enlarge the scroll content.
        for label in self.findChildren(QLabel):
            label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.scope.currentIndexChanged.connect(self.refresh)
        self.scope.activated.connect(lambda *_: self.refresh())
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.value.valueChanged.connect(self.value_changed)

    def _toggle_anomalies(self):
        self.refresh()
        self.anomalies.setVisible(self.anomalies.isHidden())

    def mode_changed(self):
        self.value.blockSignals(True)
        self.value.setValue(max(1, self.values[self.mode.currentData()]))
        self.value.blockSignals(False)
        self.value_changed()

    def value_changed(self):
        mode = self.mode.currentData()
        self.values[mode] = self.value.value()
        self.owner._set_setting("txt_split", {"mode": mode, **self.values})
        self.refresh()

    def choose_categories(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("指定分类")
        layout = QVBoxLayout(dialog)
        choices = QListWidget()
        for category in CATEGORIES:
            entry = QListWidgetItem(category)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable)
            entry.setCheckState(Qt.Checked if category in self.categories else Qt.Unchecked)
            choices.addItem(entry)
        layout.addWidget(choices)
        ok = QPushButton("确定")
        ok.clicked.connect(dialog.accept)
        layout.addWidget(ok)
        if dialog.exec() == QDialog.Accepted:
            self.categories = [choices.item(i).text() for i in range(choices.count()) if choices.item(i).checkState() == Qt.Checked]
            self.refresh()

    def show_material_help(self):
        from .ai_rule_materials import HELP_FILE, material_text
        from PySide6.QtWidgets import QDialog, QVBoxLayout, QPushButton
        dialog = QDialog(self); dialog.setWindowTitle('使用说明'); dialog.resize(650,480)
        layout = QVBoxLayout(dialog); text = QPlainTextEdit(); text.setReadOnly(True)
        text.setPlainText(material_text(HELP_FILE)); layout.addWidget(text)
        close = QPushButton('关闭'); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec()

    def export_materials(self):
        from .ai_rule_materials import export_materials
        path,_ = QFileDialog.getSaveFileName(self,'导出规则资料包','漫画AI审核规则资料包_V1.zip','ZIP (*.zip)')
        if not path: return
        try: export_materials(path)
        except OSError as exc:
            QMessageBox.warning(self,'规则资料未导出',str(exc)); return
        self.owner._show_status_message('规则资料包已导出。',4000)

    def copy_project_instructions(self):
        from .ai_rule_materials import PROJECT_FILE,material_text
        try: text = material_text(PROJECT_FILE)
        except OSError as exc:
            QMessageBox.warning(self,'项目指令不可读',str(exc)); return
        QApplication.clipboard().setText(text)
        self.owner._show_status_message('项目指令已复制。',4000)

    def copy_submission(self):
        from .ai_rule_materials import SUBMISSION
        QApplication.clipboard().setText(SUBMISSION)
        self.owner._show_status_message('送审提示词已复制。',4000)

    def export(self):
        self.owner.start_txt_from_scope(self.scope.currentData(), self.categories,
                                       self.mode.currentData(), self.value.value())

    def choose_export_directory(self):
        directory = self.owner._txt_choose_parent('修改TXT导出文件夹')
        if not directory:
            return False
        self.owner._set_setting('txt_parent', directory)
        self.refresh_directory()
        return True

    def choose_results(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "选择RESULT文件（可多选）", "", "TXT文件 (*.txt);;所有文件 (*)")
        if paths:
            self.owner.import_txt_files(paths)

    def recent_task_id(self):
        tasks = [t for t in self.owner.review_tasks() if is_txt(t)]
        recent = self.owner.settings_data.get('txt_recent_task')
        return recent if any(t['task_id']==recent for t in tasks) else (tasks[-1]['task_id'] if tasks else None)

    def choose_directory(self):
        if self.directory_busy:
            return False
        directory = QFileDialog.getExistingDirectory(self, '选择GPT结果目录', str(self.owner.settings_data.get('txt_result_directory','')))
        if not directory:
            return False
        self.owner._set_setting('txt_result_directory', directory)
        self.refresh_directory()
        dialog = getattr(self.owner,'_ai_dialog',None)
        if dialog:
            dialog._refresh_directory_labels()
        return True

    def refresh_directory(self):
        if hasattr(self,'archive_controls'): self.archive_controls.refresh()
        def show(label, text):
            label.setText(QFontMetrics(self.font()).elidedText(text,Qt.ElideMiddle,max(100,label.width())))
            label.setToolTip(text)
        set_address(self.directory_label, str(self.owner.settings_data.get('txt_result_directory') or ''))
        set_address(self.export_directory, str(self.owner.settings_data.get('txt_parent') or ''), '首次导出时选择')
        show(self.directory_target,'默认导入最近导出的TXT任务：'+str(self.recent_task_id() or '尚无TXT任务'))
        if self.directory_status:
            self.summary.setText(self.directory_status)
        self.directory_button.setEnabled(bool(self.recent_task_id()) and not self.directory_busy)
        self.directory_change.setEnabled(not self.directory_busy)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'directory_label'):
            self.refresh_directory()

    def import_directory(self, task_id):
        if self.directory_busy:
            return
        task = self.owner.review_task(task_id) if task_id else None
        if task is None or not is_txt(task):
            return
        if not self.owner.settings_data.get('txt_result_directory') and not self.choose_directory():
            return
        # Capture the identity now. Changing tabs or selected tasks cannot retarget this operation.
        directory = str(self.owner.settings_data['txt_result_directory'])
        self.directory_busy = True
        self.directory_target_id = task_id
        self.directory_status = '正在读取结果目录｜任务 ' + task_id
        self.refresh_directory()
        dialog = getattr(self.owner,'_ai_dialog',None)
        if dialog:
            dialog.directory_import_btn.setEnabled(False)
            dialog._refresh_directory_labels()
        def work():
            try:
                result = scan_results(directory,task_id,lambda done,total:self.directoryProgress.emit(done,total) if done==1 or done==total or done%10==0 else None)
            except Exception as exc:
                result = {'task_id':task_id,'files':[],'other_tasks':0,'scanned':0,'skipped':1,'anomalies':[{'status':'目录读取失败','reason':_redact(str(exc))}]}
            self.directoryRead.emit(result)
        threading.Thread(target=work,daemon=True).start()

    def _directory_progress(self, done, total):
        self.directory_status = f'任务 {self.directory_target_id}｜正在读取结果目录 {done}/{total} 个TXT'
        self.refresh_directory()
        dialog = getattr(self.owner,'_ai_dialog',None)
        if dialog:
            dialog._refresh_directory_labels()

    def _directory_finished(self, result):
        try:
            task = self.owner.review_task(result['task_id'])
            if task is None or not is_txt(task):
                raise ValueError('原目标TXT任务已不可用，本次未导入；请重新选择任务')
            report = self.owner.import_txt_texts(result['files'], sources=result.get('sources',{}))
            report['directory'] = {k:result[k] for k in ('task_id','scanned','other_tasks','skipped')}
            report['anomalies'].extend(result['anomalies'])
            self.owner._show_txt_report(report)
            self.directory_status = f"任务 {result['task_id']}｜审核成功 {report['success']}｜重复 {report['duplicate_files']} 份｜其他任务跳过 {result['other_tasks']} 份｜问题 {result['skipped'] + report['file_errors']} 份"
            from .ai_archive import archive_line
            if archive_line(report): self.directory_status += '｜'+archive_line(report)
        except Exception as exc:
            self.directory_status = '任务 ' + result['task_id'] + '｜目录导入未完成：' + _redact(str(exc))
        finally:
            self.directory_busy = False
            self.refresh_directory()
            dialog = getattr(self.owner,'_ai_dialog',None)
            if dialog:
                dialog.refresh()

    def open_folder(self):
        tasks = [t for t in self.owner.review_tasks() if is_txt(t)]
        if tasks:
            self.owner.open_txt_folder(self.owner.settings_data.get("txt_recent_task", tasks[-1]["task_id"]))

    def refresh(self):
        self.refresh_directory()
        scope = self.scope.currentData()
        self.category_btn.setEnabled(scope == "categories")
        self.scope.model().item(self.scope.findData("search")).setEnabled(True)
        count = len(self.owner.ai_items_for_scope(scope, self.categories))
        self.range_label.setText(f"本次范围：{count} 本｜启用补充依据 {len(enabled_supplements(self.owner.settings_data))} 条")
        sizes = split_sizes(count, self.mode.currentData(), self.value.value())
        if sizes:
            detail = f"每份 {sizes[0]} 本，末份 {sizes[-1]} 本" if self.mode.currentData() == "books" else f"每份约 {min(sizes)}～{max(sizes)} 本"
            self.preview.setText(f"预计 {len(sizes)} 份｜{detail}｜不生成空份")
        else:
            self.preview.setText("当前范围没有可复核的漫画")
        self.export_btn.setEnabled(bool(count))
        tasks = [t for t in self.owner.review_tasks() if is_txt(t)]
        if tasks:
            recent = self.owner.settings_data.get("txt_recent_task")
            task = next((t for t in tasks if t["task_id"] == recent), tasks[-1])
            completed = sum(p["state"] == "exported" for p in task["parts"])
            cancelled = sum(p["state"] == "cancelled" for p in task["parts"])
            pending = [p for p in task["parts"] if p["state"] == "export_pending"]
            feedback = f"最近任务：{len(task['items'])} 本｜已导出 {completed}/{len(task['parts']) - cancelled} 份"
            if pending:
                feedback += f"｜待重试 {len(pending)} 份\n请到任务页重试未导出分片。" + _redact(str(pending[0].get("reason", "")))[:250]
            feedback = '最近任务：'+task['task_id']+'｜'+feedback.removeprefix('最近任务：')
            self.export_note.setText(feedback)
            self.export_note.setToolTip(feedback)
        else:
            self.export_note.setText('最近任务：暂无')
        report = getattr(self.owner, "_last_txt_report", None)
        if report and not self.directory_busy:
            self.summary.setText(f"文件 {report['files']}｜审核成功 {report['success']}（建议名已应用 {report['applied']}）｜失败 {report['failed']}｜过期 {report['stale']}\n"
                                 f"旧轮次忽略 {report['superseded']}｜重复结果 {report['duplicate']} 本（重复文件 {report['duplicate_files']} 份）｜不存在 {report['missing']}｜待核对 {report['deferred']}｜文件错误 {report['file_errors']}")
            directory = report.get('directory')
            if directory:
                self.summary.setText(self.summary.text() + f"\n最近导入任务 {directory['task_id']}｜目录扫描 {directory['scanned']} 份｜其他任务跳过 {directory['other_tasks']} 份｜读取/未完整等问题 {directory['skipped']} 份")
            labels = {**ROW_LABELS, "stale": "结果过期", "success": "建议未应用"}
            from .ai_archive import archive_line
            if archive_line(report): self.summary.setText(self.summary.text()+'\n'+archive_line(report))
            groups = {}
            for row in report["anomalies"]:
                status = labels.get(row["status"], row["status"])
                groups.setdefault(status, []).append("｜".join(str(row.get(k, "")) for k in ("title", "file", "reason") if row.get(k)))
            self.anomalies.setPlainText(_redact("\n\n".join(k + "\n" + "\n".join(v) for k, v in groups.items())))
        elif not report:
            self.anomalies.clear()
        self.anomaly_toggle.setEnabled(True)
