"""TXT exports/imports and task-bound remaining comic actions."""
from __future__ import annotations

import os
import json
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .ai_txt import MAX_RESULT_BYTES, counts
from .ai_txt_store import (TxtRepository, TxtSession, is_txt, create_task,
                           export_parts, supplement, import_results, accessible)
from .ai_supplements import enabled_supplements, current_input_fingerprint
from .run_log import _redact, log_message


class TxtController:
    def txt_repository(self):
        if not hasattr(self, "_txt_repository"):
            self._txt_repository = TxtRepository(self.sessions_dir())
            self._txt_repository.scan()
        return self._txt_repository

    def review_tasks(self):
        return self.txt_repository().tasks(self.ai_tasks)

    def review_task(self, task_id):
        return next((t for t in self.review_tasks() if t.get("task_id") == task_id), None)

    def _txt_context(self, task_id=None):
        if task_id is None or self._task_by_id(task_id) is not None:
            self._commit_active_edit()
            self._finish_background_session_save()
            def commit(context):
                commit_id = task_id or (context.tasks[-1].get("task_id") if context.tasks else None)
                self._save_current_session(strict=True, stage="TXT任务和结果", task_id=commit_id)
            self._ensure_library()
            context = TxtSession({"session_id": self.session_id, "library_id": self.library_id, "ai_tasks": self.ai_tasks,
                               "work_directory": (self._loaded_source or ('', self.work_edit.text().strip()))[1]},
                              self.all_items, commit)
            context.file_blocked_ids = self.active_file_ids()
            return context
        context = self.txt_repository().context(task_id)
        from .library_identity import LibraryRegistry
        old_library = LibraryRegistry(self.tool_dir()).resolve(context.payload.get('source_database', ''),
            context.payload.get('work_directory', ''), context.payload.get('library_id', ''))
        if not self.all_items or not self.library_id or old_library != self.library_id:
            raise ValueError('历史库任务只读；请先加载它所属的原漫画库，再导入或处理结果')
        context.payload['library_id'] = old_library
        self.file_store().reconcile_items(context.items, old_library, context.payload['session_id'], True)
        if self._file_runner:
            locked_paths = {r['source_path'] for r in self._file_runner.task['rows']} | {r['target_path'] for r in self._file_runner.task['rows']}
            context.file_blocked_ids = {item.local_id for item in context.items if item.original_path in locked_paths}
        return context

    def _txt_overlap(self, context, items):
        active = {i for t in context.tasks for i, r in t.get("items", {}).items()
                  if r.get("state") in {"pending", "dispatching", "uncertain", "export_pending"}}
        overlap = active.intersection(i.local_id for i in items)
        if not overlap:
            return items
        box = QMessageBox(self)
        box.setWindowTitle("有漫画正在审核")
        box.setText(f"其中 {len(overlap)} 本与未完成的审核重叠。全部继续后，这些漫画以新任务为准，旧结果晚到将忽略。")
        all_button = box.addButton("全部继续", QMessageBox.AcceptRole)
        skip = box.addButton("只发无重叠项", QMessageBox.ActionRole)
        box.addButton("取消", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is all_button:
            return items
        if box.clickedButton() is skip:
            return [i for i in items if i.local_id not in overlap]
        return []

    def _txt_choose_parent(self, title="选择TXT导出父目录"):
        start = self.settings_data.get("txt_parent", "")
        if start and not os.path.isdir(start):
            QMessageBox.information(self, "导出目录不可用", "上次导出目录当前不可用，请重新选择父目录。")
            start = ""
        return QFileDialog.getExistingDirectory(self, title, start)

    def _txt_export_parent(self):
        directory = str(self.settings_data.get('txt_parent') or '')
        if directory and os.path.isdir(directory):
            return directory
        return self._txt_choose_parent()

    def _txt_after_export(self, context, task):
        export_parts(context, task)
        self._set_setting("txt_recent_task", task["task_id"])
        self._ai_log(task["task_id"]).event("txt_export", counts=counts(task),
                                           parts=len(task["parts"]), directory=task["export_dir"])
        self.txt_repository().scan()
        self._refresh_ai_dialog()
        self._show_status_message(f"TXT任务已保存：{len(task['items'])} 本，{len(task['parts'])} 份；请查看导出结果。", 6000)

    def start_txt_from_scope(self, scope, categories=None, mode="books", value=10, parent=None):
        from .pagination import sort_items
        try:
            context = self._txt_context()
            items = sort_items({i.local_id: i for i in self.ai_items_for_scope(scope, categories)}.values())
            items = self._filter_ai_file_overlap(items)
            items = self._txt_overlap(context, items)
            if not items:
                return
            parent = parent or self._txt_export_parent()
            if not parent:
                return
            self._set_setting("txt_parent", parent)
            task = create_task(context, items, parent, mode, value, scope, enabled_supplements(self.settings_data))
            if self._ai_runner:
                for i in items:
                    self._ai_runner.superseded_ids.add(i.local_id)
            self._txt_after_export(context, task)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "TXT导出未完成", _redact(str(exc)))
            self._refresh_ai_dialog()

    def retry_txt_export(self, task_id, parent=None):
        try:
            context = self._txt_context(task_id)
            task = next(t for t in context.tasks if t["task_id"] == task_id)
            if not any(p['state']=='export_pending' for p in task['parts']):
                return
            old_directory = Path(task.get("last_export_dir", task["export_dir"]))
            if parent is None:
                choice = QFileDialog.getExistingDirectory(self, "选择重新导出位置（仅处理未导出文件）", str(old_directory))
                if not choice:
                    return
                parent = None if Path(choice).resolve()==old_directory.resolve() else choice
            export_parts(context, task, parent)
            self._set_setting("txt_recent_task", task["task_id"])
            self.txt_repository().scan()
            self._refresh_ai_dialog()
        except (OSError, ValueError, StopIteration) as exc:
            QMessageBox.warning(self, "导出记录尚未确认保存", _redact(str(exc)))

    def supplement_txt(self, task_id, selected, mode, value, parent=None):
        try:
            context = self._txt_context(task_id)
            task = next(t for t in context.tasks if t["task_id"] == task_id)
            candidates = [context.by_id[i] for i in (selected or list(task['items'])) if i in context.by_id]
            candidates = self._filter_ai_file_overlap(candidates)
            selected = [i.local_id for i in candidates]
            if not selected:
                return
            # Reuse the original task directory; only ask again if unavailable.
            if parent is None and not Path(task.get("last_export_dir", task["export_dir"])).is_dir():
                parent = self._txt_choose_parent("原目录不可用，选择补审TXT导出父目录")
                if not parent:
                    return
            eligible, expired = supplement(context, task, selected, mode, value)
            if eligible:
                export_parts(context, task, parent)
                self._set_setting("txt_recent_task", task["task_id"])
            if expired:
                QMessageBox.information(self, "部分项目需要重新复核", f"{len(expired)} 本资料已变化，不放入原任务补审。请在漫画明细中勾选后，右键“重新复核资料变化项”开始新任务。")
            if not eligible and not expired:
                QMessageBox.information(self, "没有可补审项目", "所选项目已成功、被接替或不存在；未重复送审。")
            self.txt_repository().scan()
            self._refresh_ai_dialog()
        except (OSError, ValueError, StopIteration) as exc:
            QMessageBox.warning(self, "补审未完成", _redact(str(exc)))
            self._refresh_ai_dialog()

    def api_remaining_to_txt(self, task_id, mode="books", value=10, parent=None, selected=None):
        """New TXT round, inheriting the API task's frozen supplemental evidence."""
        from .pagination import sort_items
        try:
            if self._ai_runner and self._ai_runner.task_id == task_id and self._ai_runner.is_alive():
                raise ValueError("请先停止本API任务，待已发送组保存后再导出剩余项")
            context = self._txt_context(task_id)
            old = next(t for t in context.tasks if t["task_id"] == task_id)
            if not accessible(context.payload.get("work_directory", "")):
                raise OSError("原工作目录暂不可访问，恢复后再导出")
            from .ai_remaining import remaining_ids
            items = [context.by_id[i] for i in remaining_ids(old, context.by_id, selected)]
            items = self._filter_ai_file_overlap(items)
            if not items:
                raise ValueError("本任务没有仍属当前轮次的剩余漫画")
            parent = parent or self._txt_choose_parent("选择API剩余项的TXT导出父目录")
            if parent:
                task = create_task(context, sort_items(items), parent, mode, value, "api_remaining", old.get("supplements", []), continuation=task_id)
                self._txt_after_export(context, task)
        except (OSError, ValueError, StopIteration) as exc:
            QMessageBox.warning(self, "剩余项导出未完成", _redact(str(exc)))
            self._refresh_ai_dialog()

    def rereview_txt(self, task_id, selected, mode, value, parent=None):
        from .ai_review import business_input, fingerprint
        from .pagination import sort_items
        try:
            context = self._txt_context(task_id)
            old = next(t for t in context.tasks if t["task_id"] == task_id)
            if not accessible(context.payload.get("work_directory", "")):
                raise OSError("原工作目录暂不可访问，恢复后再重新复核")
            items = []
            for local_id in selected:
                item, row = context.by_id.get(local_id), old["items"].get(local_id)
                if item and row and row["state"] in {"pending", "failed"} and accessible(item.original_path):
                    if current_input_fingerprint(item, row["input"]) != row["input"]["CONTROL"]["input_fingerprint"]:
                        items.append(item)
            items = self._txt_overlap(context, self._filter_ai_file_overlap(sort_items(items)))
            if not items:
                QMessageBox.information(self, "没有资料变化项", "只对原输入已经变化的选中项目建立新任务；其余可用原任务补审。")
                return
            parent = parent or self._txt_choose_parent()
            if parent:
                task = create_task(context, items, parent, mode, value, "rereview", enabled_supplements(self.settings_data))
                self._txt_after_export(context, task)
        except (OSError, ValueError, StopIteration) as exc:
            QMessageBox.warning(self, "重新复核未完成", _redact(str(exc)))

    def import_txt_files(self, paths):
        from .ai_archive import read_source
        files, errors, sources = [], [], {}
        for path in paths:
            try:
                path = Path(path)
                text,token = read_source(path)
                files.append((str(path), text)); sources[str(path)] = token
            except (OSError, ValueError, UnicodeError) as exc:
                errors.append({"file": _redact(str(path)), "status": "文件跳过", "reason": _redact(str(exc))})
        report = self.import_txt_texts(files, sources=sources)
        report["files"] += len(errors)
        report["file_errors"] += len(errors)
        report["anomalies"].extend(errors)
        if errors:
            log_message("TXT文件读取异常：" + json.dumps(errors, ensure_ascii=False), "WARNING")
        self._show_txt_report(report)
        return report

    def import_txt_texts(self, files, sources=None):
        self._commit_active_edit()
        self._finish_background_session_save()
        repository = self.txt_repository()
        repository.scan()
        from .ai_txt import parse_result
        touched = set()
        for filename, text in files:
            try:
                parsed = parse_result(text)
                tid = parsed["meta"].get("task_id")
                if self.review_task(tid) is not None:
                    touched.add(tid)
                    self._ai_log(tid).file("result_" + parsed["digest"][:24] + ".txt", parsed["text"].encode(), "TXT原始结果")
            except (ValueError, OSError):
                pass
        anchor = self._capture_page_anchor()
        report = import_results(repository, files, self._txt_context)
        from .ai_archive import archive_results
        archive_results(report,files,sources or {},self.settings_data)
        file_anomalies = [row for row in report["anomalies"] if row.get("status") in {"文件跳过", "未确认保存", "待核对文件未保存"}]
        if file_anomalies:
            log_message("TXT文件导入异常：" + json.dumps(file_anomalies, ensure_ascii=False), "WARNING")
        # Exactly one primary-list refresh per user import, including mixtures.
        self.populate_categories(anchor=anchor, preserve_current=True, background_ai=True)
        self.show_scan_summary()
        repository.scan()
        self._refresh_ai_dialog()
        for task_id in touched:
            self._ai_log(task_id).event("txt_import_summary", batch={k: v for k, v in report.items() if k != "anomalies"},
                                      task_counts=counts(self.review_task(task_id)))
        report['task_ids'] = sorted(touched)
        return report

    def retry_deferred_txt(self):
        try:
            files = self.txt_repository().deferred()
            report = self.import_txt_texts(files)
            self._show_txt_report(report)
            return report
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "待核对结果暂不可读", _redact(str(exc)))

    def _show_txt_report(self, report):
        self._last_txt_report = report
        if self._ai_dialog is not None and not report.get('directory') and not self._ai_dialog.txt_page.directory_busy:
            self._ai_dialog.txt_page.directory_status = ''
        self._refresh_ai_dialog()

    def open_txt_folder(self, task_id):
        task = self.review_task(task_id)
        if task is not None:
            path = Path(task.get("last_export_dir", task["export_dir"]))
            if path.is_dir():
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
            else:
                QMessageBox.information(self, "导出目录不可用", "原目录已移动或暂不可访问；导出记录仍保留。")
