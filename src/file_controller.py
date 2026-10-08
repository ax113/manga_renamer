"""Main-window integration for file operations; all disk work lives in file_tasks."""
from __future__ import annotations
import copy
import os
from pathlib import Path

from PySide6.QtCore import QObject, Signal, QTimer, Qt
from PySide6.QtWidgets import QMessageBox
from .library_identity import LibraryRegistry
from .file_tasks import (FileTaskStore, FileTaskRunner, object_identity, same_identity,
                         preflight, counts, ROW_LABELS, identity_key)
from .session_store import item_to_dict
from .scanner import strip_archive_suffix
from .pagination import sort_items
from .path_rules import path_key
from .file_operations import file_labels, result_status, within
from .file_undo import undo_plans
from .file_queue import FileQueue


class FileSignals(QObject):
    result = Signal(str, dict)
    done = Signal(str)
    progress = Signal(str, dict)


class FileController:
    def _init_file_controller(self):
        self.library_id = ''
        self._loaded_source = None
        self._file_store = None
        self._file_runner = None
        self._file_queue = None
        self._file_progress = {}
        self._file_dialog = None
        self._tasks_exit_pending = False
        self._tasks_closing = False
        self._file_signals = FileSignals(self)
        self._file_signals.result.connect(self._on_file_result)
        self._file_signals.done.connect(self._on_file_done)
        self._file_signals.progress.connect(self._on_file_progress)
        self._file_refresh_timer = QTimer(self)
        self._file_refresh_timer.setSingleShot(True)
        self._file_refresh_timer.setInterval(150)
        self._file_refresh_timer.timeout.connect(self._refresh_after_file_change)
        self._file_recovery_offered = False

    def file_store(self):
        if self._file_store is None:
            self._file_store = FileTaskStore(self.tool_dir(), self.review_log_root())
        return self._file_store

    def file_queue(self):
        if self._file_queue is None:
            self._file_queue=FileQueue(self.file_store())
        return self._file_queue

    def _reserved_file_tasks(self):
        result=list(self.file_queue().waiting())
        if self._file_runner: result.append(self._file_runner.task)
        active=self.file_store().tasks.get(self.file_queue().active_id)
        if active and active not in result: result.append(active)
        return result

    def continue_file_queue(self):
        if self.file_busy():
            return
        try:
            task = self.file_queue().control_task()
            if task is None:
                return
            if task['library_id'] != self.library_id or not self._loaded_source:
                raise ValueError('请先加载暂停任务所属的漫画库，再继续')
            if task['state'] != 'queued':
                self.file_store().recover(self.library_id)
                c = counts(task)
                if c['failed'] or c['blocked'] or c['uncertain']:
                    self.open_file_dialog(task['task_id'])
                    raise ValueError('当前任务有异常；请在明细下方重试失败项，或重新核对阻断 / 待确认项，再点击上方继续')
                if c['pending'] or c['unexecuted']:
                    self.resume_file_task(task['task_id'], continue_queue=True)
                    return
            self.file_queue().resume()
            self._start_next_file_task()
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,'队列未继续',str(exc))
        if self._file_dialog: self._file_dialog.refresh()

    def cancel_queued_file_task(self, task_id):
        try:
            self.file_queue().cancel(task_id)
            if self._file_dialog: self._file_dialog.refresh()
            self._refresh_library_lock_ui()
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,'排队任务未取消',str(exc))

    def resume_legacy_file_task(self, task_id):
        """Adopt an explicitly selected old task only when the queue is idle."""
        if self.file_busy():
            return
        try:
            queue = self.file_queue()
            task = self.file_store().tasks.get(task_id)
            if not task or task['state'] in {'completed', 'cancelled', 'queued'}:
                raise ValueError('该记录目前不是可恢复的旧任务，请重新核对')
            if not self._loaded_source or task['library_id'] != self.library_id:
                raise ValueError('请先加载旧任务所属的漫画库')
            if self.file_store().errors:
                raise ValueError('任务记录读取异常，请先核对记录')
            if queue.control_task() is not None:
                raise ValueError('请先处理当前任务及等待队列，再继续此旧任务')
            self.file_store().recover(self.library_id)
            c = counts(task)
            if c['failed'] or c['blocked'] or c['uncertain']:
                raise ValueError('旧任务有异常，请先重试失败项或重新核对，再继续')
            if c['pending'] or c['unexecuted']:
                self.resume_file_task(task_id, continue_queue=True)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '旧任务未继续', str(exc))
        if self._file_dialog:
            self._file_dialog.refresh()

    def _start_next_file_task(self):
        if self.file_busy() or self._tasks_exit_pending or self._tasks_closing: return
        task=self.file_queue().next(self.library_id)
        if task is None: return
        acquired=False
        try:
            self.file_store().acquire_execution(); acquired=True
            self._launch_file_task(task)
        except (OSError,ValueError) as exc:
            if acquired: self.file_store().release_execution()
            self.file_queue().pause()
            self._show_status_message('队列暂停：'+str(exc),15000)

    def _on_file_progress(self, task_id, progress):
        self._file_progress=dict(progress,task_id=task_id)
        if self._file_dialog: self._file_dialog.update_progress()

    def _bind_library(self, database, directory, preferred=''):
        self.library_id = LibraryRegistry(self.tool_dir()).resolve(database, directory, preferred)
        self._loaded_source = (str(database), str(directory))
        return self.library_id

    def _ensure_library(self):
        if not self.library_id and self.all_items:
            self._bind_library(self.db_edit.text().strip(), self.work_edit.text().strip())
        return self.library_id

    def file_busy(self):
        return self._file_runner is not None

    def active_library_tasks(self):
        return self.file_busy() or (bool(self.file_queue().waiting()) and not self.file_queue().paused) or self._ai_runner is not None or bool(self._ai_secret_configs)

    def _reserved_file_identities(self):
        return {identity_key(identity) for task in self._reserved_file_tasks() for row in task['rows'] if row['state'] not in {'blocked','success'} for entry in [row]+row.get('steps',[]) for identity in (entry.get('identity'),entry.get('result_identity')) if identity}

    def active_file_ids(self):
        identities=self._reserved_file_identities()
        recorded={r['local_id'] for task in self._reserved_file_tasks() for r in task['rows'] if r['state'] not in {'blocked','success'}}
        return recorded | {i.local_id for i in self.all_items if i.file_identity and identity_key(i.file_identity) in identities}

    def active_ai_ids(self):
        active = {'running', 'queued', 'paused', 'interrupted', 'waiting', 'export_failed'}
        path_ids = {i.original_path: i.local_id for i in self.all_items}
        result = set()
        self.txt_repository().scan()
        for task in self.review_tasks():
            if task.get('state') not in active:
                continue
            if task not in self.ai_tasks and not self.review_task_is_current_library(task):
                continue
            if task.get('library_id') and task['library_id'] != self.library_id:
                continue
            for i, row in task.get('items', {}).items():
                if row.get('state') in {'pending', 'dispatching', 'uncertain', 'export_pending'}:
                    if task.get('session_id') == self.session_id:
                        result.add(i)
                    if row.get('original_path') in path_ids:
                        result.add(path_ids[row['original_path']])
        return result

    def _filter_ai_file_overlap(self, items):
        blocked = self.active_file_ids()
        identities = self._reserved_file_identities()
        excluded = {i.local_id for i in items if i.local_id in blocked or (i.file_identity and identity_key(i.file_identity) in identities)}
        if excluded:
            QMessageBox.information(self, '部分漫画正在文件处理',
                f'{len(excluded)} 本正在文件任务中，已从本次 AI 复核中排除；其余漫画正常送审。')
        return [i for i in items if i.local_id not in excluded]

    def review_task_is_current_library(self, task):
        if not task:
            return False
        if not self.all_items or not self.library_id:
            return False
        identity = task.get('library_id')
        if identity:
            return identity == self.library_id
        if self._task_by_id(task.get('task_id')) is not None:
            return True  # Legacy tasks in the actual loaded session are bound on restore.
        source = self.txt_repository().library_source(task.get('task_id'))
        if source is None:
            return False
        if source['library_id']:
            return source['library_id'] == self.library_id
        # A display-only legacy label needs the source pair, not every saved comic.
        # Mutating actions still use context() and verify fresh authoritative bytes.
        from .library_identity import canonical
        loaded = self._loaded_source
        return bool(loaded and source['work_directory'] and
            canonical(source['source_database']) == canonical(loaded[0]) and
            canonical(source['work_directory']) == canonical(loaded[1]))

    def _block_library_change(self):
        if self.active_library_tasks():
            QMessageBox.information(self, '任务正在运行', '请先停止 AI / 文件任务并确认保存状态，再重扫、恢复会话或切换数据库 / 工作目录。')
            return True
        return False

    def open_file_dialog(self, task_id=''):
        from .file_dialog import FileDialog
        if self._file_dialog is None:
            self._file_dialog = FileDialog(self)
        self._file_dialog.refresh()
        if task_id:
            self._file_dialog.show_task(task_id)
        elif self.file_busy():
            self._file_dialog.show_task(self._file_runner.task['task_id'])
        if self._file_dialog.isMinimized():
            self._file_dialog.showNormal()
        else:
            self._file_dialog.show()
        self._file_dialog.raise_()
        self._file_dialog.activateWindow()

    def file_preview(self, scope, categories, exclude, action='rename', destination=''):
        self._commit_active_edit()
        items = self.ai_items_for_scope(scope, categories)
        if exclude and action == 'rename':
            items = [i for i in items if '已改名' not in file_labels(i)]
        items = sort_items(items)
        destination_problem = '目标目录必须是完整绝对路径，请重新选择' if destination and not os.path.isabs(destination) else ''
        if action == 'move':
            destination = os.path.abspath(destination) if destination else ''
            base = [{'item':i, 'local_id':i.local_id, 'current_name':Path(i.original_path).name,
                'final_name':Path(i.original_path).name, 'target_path':os.path.join(destination,Path(i.original_path).name) if destination else i.original_path,
                'changed':bool(destination and os.path.join(destination,Path(i.original_path).name)!=i.original_path),
                'blocking_conflict':False, 'message':'-'} for i in items]
        else:
            base = self._build_operation_rows(items)
        ai_ids = self.active_ai_ids()
        reserved=self.active_file_ids()
        directories = {}
        for row in base:
            item = row['item']
            plan = {'local_id': item.local_id, 'session_id': self.session_id,
                    'source_path': item.original_path, 'target_path': row['target_path'],
                    'current_name': row['current_name'], 'final_name': row['final_name'],
                    'previous_status': item.file_status, 'identity': copy.deepcopy(item.file_identity),
                    'item_snapshot': item_to_dict(item), 'plan_problem': '', 'operation':action,
                    'tracking_identity':copy.deepcopy(item.extra.get('file_tracking_identity',item.file_identity)),
                    'result_status':result_status(file_labels(item),action)}
            if action=='move':
                plan['plan_problem'] = destination_problem
                try: plan['target_parent_identity']=object_identity(destination) if destination else {}
                except (OSError,ValueError) as exc: plan['plan_problem']=str(exc)
                if not destination: plan['plan_problem']='请在底部选择目标目录'
                elif any(i.file_identity.get('kind')=='directory' and within(destination,i.original_path) for i in self.all_items):
                    plan['plan_problem']='目标目录位于已扫描漫画内部，不能移动到漫画内'
                elif any(i is not item and (within(i.original_path,item.original_path) or within(item.original_path,i.original_path)) for i in items):
                    plan['plan_problem']='本批含相互嵌套的漫画目录，请分别处理'
            try:
                if not plan['identity']:
                    plan['identity'] = object_identity(item.original_path)
                    item.file_identity = copy.deepcopy(plan['identity'])
                if row['blocking_conflict']:
                    plan['plan_problem'] = row['message']
                reason = '本漫画已在运行 / 排队任务中，请先完成或取消该任务' if item.local_id in reserved else preflight(plan, ai_ids, directories, allow_noop=True)
            except (OSError, ValueError) as exc:
                reason = str(exc)
            row['plan'], row['blocking_conflict'] = plan, bool(reason)
            row['executable'] = not reason and row['changed']
            row['status'] = '冲突' if reason else ('可执行' if row['changed'] else '无需执行')
            row['message'] = reason or ('-' if row['changed'] else ('位置未变，无需移动' if action=='move' else '名称不变，无需执行'))
            row['file_labels'] = file_labels(item)
            if action=='move' and not reason and not row['changed']: row['status']='无需移动'
        groups = {}
        for row in base:
            groups.setdefault(path_key(row['target_path']), []).append(row)
        number = 0
        for members in groups.values():
            if len(members) < 2:
                continue
            number += 1
            group = f'C{number:02d}'
            for row in members:
                message = f'{group} 本组共 {len(members)} 本，最终目标相同'
                if not row['changed']:
                    message = ('本项位置未变；' if action=='move' else '本项名称未变；') + message
                if row['message'] != '-' and '本批' not in row['message']:
                    message += '；' + row['message']
                row.update(conflict_group=group, conflict_count=len(members),
                    conflict_color=(number - 1) % 2, blocking_conflict=True,
                    executable=False, status='冲突', message=message)
                row['plan']['plan_problem'] = message
        return base

    def start_file_task(self, plans, action='rename'):
        if not plans or (action!='move' and (self.file_busy() or self.file_queue().waiting())):
            return
        store = self.file_store()
        acquired=False
        try:
            self._ensure_library()
            by_id = {i.local_id: i for i in self.all_items}
            plans = copy.deepcopy(plans)
            for plan in plans:
                item = by_id.get(plan['local_id'])
                target = os.path.join(os.path.dirname(plan['target_path']),Path(item.original_path).name) if item and action=='move' else (self._target_path_for_item(item) if item else '')
                if not item or item.original_path != plan['source_path'] or target != plan['target_path']:
                    plan['plan_problem'] = '预览后漫画路径或最终名称已变化，请重新检查'
            if set(p['local_id'] for p in plans) & self.active_file_ids():
                raise ValueError('所选漫画已在运行 / 排队任务中，请重新检查')
            if not self.file_busy():
                store.acquire_execution(); acquired=True
            task = store.create(self.library_id, self.session_id,
                (self._loaded_source or ('', self.work_edit.text().strip()))[1], plans, action)
            self.file_queue().submit(task)
            if acquired: store.release_execution(); acquired=False
            self._start_next_file_task()
            self.open_file_dialog(task['task_id'])
            self._refresh_library_lock_ui()
        except (OSError, ValueError) as exc:
            if acquired: store.release_execution()
            QMessageBox.warning(self, '文件任务未启动', str(exc))

    def _launch_file_task(self, task, states=('pending', 'unexecuted')):
        self.file_queue().start(task)
        # A paused queue may have survived a rescan. Bind by path AND physical
        # identity again so fresh AI locks and UI results use current local IDs.
        for row in task['rows']:
            if row['state'] not in states: continue
            entry=next((s for s in row.get('steps',[]) if s['state']!='success'),row)
            identity=entry.get('identity',row.get('result_identity',row['identity']))
            matches=[i for i in self.all_items if i.original_path==entry['source_path'] and same_identity(i.original_path,identity)]
            if not matches and entry.get('transfer'):
                target_identity=entry['transfer'].get('staging_identity')
                matches=[i for i in self.all_items if i.original_path==entry['target_path'] and same_identity(i.original_path,target_identity)]
            if len(matches)==1: row['local_id'],row['session_id']=matches[0].local_id,self.session_id
        self._file_progress={}
        self._file_runner = FileTaskRunner(self.file_store(), task, states, self.active_ai_ids(),
                                          self._file_signals.result.emit, self._file_signals.done.emit, progress=self._file_signals.progress.emit)
        self._file_runner.start()
        self._refresh_library_lock_ui()
        self._update_file_entry()
        self.open_file_dialog(task['task_id'])
        self._refresh_ai_dialog()

    def resume_file_task(self, task_id, failed=False, *, continue_queue=False):
        if self.file_busy():
            return
        store = self.file_store()
        task = store.tasks[task_id]
        if task['state']=='queued':
            self.continue_file_queue(); return
        if task['state']=='cancelled': return
        acquired=False
        chain_enabled=False
        try:
            if task['library_id'] != self.library_id or not self._loaded_source:
                raise ValueError('历史库任务只读，请先加载它所属的原漫画库')
            store.recover(self.library_id)
            if counts(task)['uncertain']:
                raise ValueError('任务中有待确认项，请先查看记录并重新核对')
            states = ('failed',) if failed else ('pending', 'unexecuted')
            if not any(r['state'] in states for r in task['rows']):
                return
            # Bind rescanned comics only by recorded object identity and current path.
            for row in task['rows']:
                if row['state'] not in states:
                    continue
                pending = next((s for s in row.get('steps',[]) if s['state']!='success'),row)
                source = pending['source_path']
                matches = [i for i in self.all_items if i.original_path == source
                           and same_identity(i.original_path, pending.get('identity',row['identity']))]
                outside_move = task['action'] in {'move','undo_move'} or row.get('steps')
                published=pending.get('transfer',{}).get('phase') in {'published','cleanup','cleanup_pending','done'}
                row['plan_problem'] = '' if published or len(matches)==1 or (outside_move and same_identity(source,pending.get('identity',row['identity']))) else '记录对象不在当前扫描范围内，未匹配其他漫画'
                if len(matches) == 1:
                    row['local_id'], row['session_id'] = matches[0].local_id, self.session_id
            store.acquire_execution(); acquired=True
            if continue_queue:
                self.file_queue().resume_for(task_id)
                chain_enabled=True
            self._launch_file_task(task, states)
        except (OSError, ValueError) as exc:
            if acquired: store.release_execution()
            if chain_enabled:
                self.file_queue().paused=True
                try: self.file_queue().save()
                except (OSError,ValueError): pass
            QMessageBox.warning(self, '任务未继续', str(exc))

    def file_undo_plans(self, task_id, include_moves=False):
        store = self.file_store()
        original = store.tasks.get(task_id)
        if not original or original['library_id'] != self.library_id or original['action'] not in {'rename','move'} or not self._loaded_source:
            raise ValueError('请先加载原漫画库，再选择改名或移动任务进行撤销')
        return undo_plans(store, original, self.all_items, self.session_id, include_moves)

    def undo_file_task(self, task_id, selected_ids=None, include_moves=None):
        if self.file_busy() or self.file_queue().waiting(): return
        store = self.file_store()
        try:
            plans = self.file_undo_plans(task_id, include_moves is True)
            original = store.tasks[task_id]
            if selected_ids is not None:
                selected = set(selected_ids)
                remaining = {plan['parent_row'] for plan in plans}
                if not selected or not selected <= remaining:
                    raise ValueError('勾选项目已变化或不可撤销，请重新选择；不会改为整任务撤销')
                plans = [plan for plan in plans if plan['parent_row'] in selected]
            if not plans: raise ValueError('本任务没有尚未撤销的成功项目')
            ai_ids, directories = self.active_ai_ids(), {}
            problems = {p['parent_row']:preflight(p,ai_ids,directories) for p in plans}
            if include_moves is None:
                from .file_undo_dialog import UndoDialog
                combined = self.file_undo_plans(task_id, True) if original['action']=='rename' and any(p.get('needs_move') for p in plans) else None
                if combined is not None:
                    chosen = {p['parent_row'] for p in plans}
                    combined = [p for p in combined if p['parent_row'] in chosen]
                combined_problems = {p['parent_row']:preflight(p,ai_ids,{}) for p in combined} if combined is not None else None
                dialog = UndoDialog(self,plans,problems,original['action'],combined,combined_problems)
                if not dialog.exec(): return
                plans, problems = dialog.selected_plans(), dialog.selected_problems()
            plans = [p for p in plans if not problems[p['parent_row']]]
            if not plans: raise ValueError('本次没有可安全撤销的漫画，未建立任务')
            store.acquire_execution()
            action = 'undo_move' if original['action']=='move' else 'undo'
            task = store.create(self.library_id,self.session_id,original['work_directory'],plans,action,task_id)
            self._launch_file_task(task)
        except (OSError,ValueError) as exc:
            store.release_execution()
            QMessageBox.warning(self,'撤销任务未启动',str(exc))

    def stop_file_task(self):
        if self._file_runner:
            self._file_runner.stop.set()
        try:
            self.file_queue().pause()
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self, '队列暂停状态未保存', str(exc))
        if self._file_runner:
            self._show_status_message('正在暂停当前任务并保存，已完成项保留；后续排队任务等待。')
        if self._file_dialog:
            self._file_dialog.refresh()

    def _on_file_result(self, task_id, row):
        task = self.file_store().tasks[task_id]
        if task['library_id'] != self.library_id:
            return
        item = next((i for i in self.all_items if i.local_id == row['local_id']), None)
        entry = next((s for s in reversed(row.get('steps',[])) if s['state']=='success'),None)
        successful = row['state']=='success' or entry is not None
        if item and successful:
            item.original_path = entry['target_path'] if entry else row['target_path']
            stem, suffix = strip_archive_suffix(Path(item.original_path).name) if row['identity']['kind'] == 'archive' else (Path(item.original_path).name, '')
            item.original_name, item.suffix = stem, suffix
            item.file_identity = copy.deepcopy((entry or row).get('result_identity',row['identity']))
            item.extra['file_tracking_identity']=copy.deepcopy(row.get('tracking_identity',row['identity']))
            item.file_status = (entry or row).get('result_status') or ('已撤销' if task['action']=='undo' else ('已撤销移动' if task['action']=='undo_move' else result_status(file_labels(item),task['action'])))
            item.extra['relative_path'] = os.path.relpath(item.original_path, task['work_directory'])
            # Preserve manual/AI names, categories and source DB records.
            item.checked = False
        elif item and row['state'] == 'failed' and task['action'] in {'rename','move'} and not file_labels(item):
            item.file_status = '执行失败'
        self._file_refresh_timer.start()

    def _refresh_after_file_change(self):
        self.populate_categories(anchor=self._capture_page_anchor(), preserve_current=True, background_ai=True)
        self.show_scan_summary()
        self._schedule_session_save()
        if self._file_dialog is not None:
            self._file_dialog.schedule_refresh()

    def _on_file_done(self, task_id):
        runner = self._file_runner
        if runner and runner.task['task_id'] == task_id:
            self._file_runner = None
        task = self.file_store().tasks[task_id]
        try:
            self.file_queue().finish(task)
        except (OSError,ValueError) as exc:
            self.file_queue().paused=True
            task['queue_warning']='队列状态保存失败，后续任务已暂停：'+str(exc)
        self._file_progress={}
        self._file_refresh_timer.stop()
        self._refresh_after_file_change()
        self._refresh_library_lock_ui()
        self._update_file_entry()
        if self._file_dialog is not None:
            self._file_dialog.refresh()
        c = counts(task)
        caption = '文件任务已暂停' if task['state']=='stopped' else '文件任务结束'
        message = f"{caption}：成功 {c['success']}，失败 {c['failed']}，执行前阻断 {c['blocked']}，未执行 {c['unexecuted']}，待确认 {c['uncertain']}"
        if task.get('queue_warning'): message+='；'+task['queue_warning']
        if task.get('persistence_error'):
            message += '；任务保存失败，请保留程序状态并核对记录'
        self._show_status_message(message, 15000)
        self.file_notice_btn.setText(f"{caption}：成功 {c['success']}，失败 {c['failed']}，阻断 {c['blocked']}　查看任务")
        self.file_notice_btn.setToolTip(message)
        self.file_notice_btn.setProperty('task_id', task_id)
        self.file_notice_btn.setVisible(self._file_dialog is None or not self._file_dialog.isVisible())
        self._maybe_exit_after_tasks()
        QTimer.singleShot(0,self._start_next_file_task)

    def _update_file_entry(self):
        if hasattr(self, 'file_process_btn'):
            self.file_process_btn.setText('改名 / 移动（执行中）' if self.file_busy() else '改名 / 移动')

    def _recover_file_items(self, items, library_id, session_id, restore=False, snapshots=None):
        store = self.file_store()
        self.file_queue()
        store.recover(library_id)
        store.reconcile_items(items, library_id, session_id, restore, snapshots)

    def _offer_file_recovery(self):
        if self._file_recovery_offered:
            return
        unfinished = [t for t in self.file_store().list() if t['state']!='cancelled' and any(r['state'] != 'success' for r in t['rows'])]
        if not unfinished:
            return
        self._file_recovery_offered = True
        task = next((t for t in unfinished if t['library_id'] == self.library_id), unfinished[0])
        c = counts(task)
        current = bool(self._loaded_source and task['library_id'] == self.library_id)
        box = QMessageBox(self)
        box.setWindowTitle('检测到未完成的文件任务' if current else '检测到历史库未完成任务')
        box.setText(f"成功 {c['success']}，失败 {c['failed']}，未执行 {c['pending']+c['unexecuted']}，待确认 {c['uncertain']}。\n不会自动续跑。")
        view = box.addButton('查看任务', QMessageBox.ActionRole)
        later = box.addButton('稍后处理', QMessageBox.RejectRole)
        resume = box.addButton('继续剩余项', QMessageBox.AcceptRole)
        resume.setEnabled(current and not c['uncertain'] and bool(c['pending'] + c['unexecuted']))
        box.setDefaultButton(later)
        box.exec()
        if box.clickedButton() is view:
            self.open_file_dialog(task['task_id'])
        elif box.clickedButton() is resume:
            self.resume_file_task(task['task_id'])

    def _toggle_hide_renamed(self, checked):
        scope = self.current_category()
        current = self._current_item()
        current_id = current.local_id if current else ''
        self.hide_renamed = bool(checked)
        for item in self.all_items:
            if checked and '已改名' in file_labels(item):
                item.checked = False
        self.populate_categories(preserve_current=True, background_ai=True)
        if current_id:
            self._restore_current_on_visible_page(scope, current_id)
        self._set_setting('hide_renamed', self.hide_renamed)

    def _guard_active_tasks_exit(self, event):
        if self._tasks_closing or not self.active_library_tasks():
            return True
        box = QMessageBox(self)
        box.setWindowTitle('仍有任务运行')
        statuses = []
        if self._ai_runner or self._ai_secret_configs:
            statuses.append('AI 复核：运行中')
        if self.file_busy():
            statuses.append('文件处理：运行中')
        if self.file_queue().waiting():
            statuses.append(f'文件队列：{len(self.file_queue().waiting())} 个任务等待；退出后保留')
        box.setText('\n'.join(statuses) + '\n退出前请选择任务处理方式。')
        keep = box.addButton('继续运行', QMessageBox.RejectRole)
        safe = box.addButton('安全停止后退出', QMessageBox.ActionRole)
        immediate = box.addButton('立即退出', QMessageBox.DestructiveRole)
        box.setDefaultButton(safe)
        box.exec()
        clicked = box.clickedButton()
        if clicked is safe:
            self._tasks_exit_pending = True
            self.stop_file_task()
            if self._ai_runner:
                self._ai_runner.stop_after_group.set()
            self._show_status_message('任务安全停止并保存后退出。', 0)
            event.ignore()
            self._maybe_exit_after_tasks()
            return False
        if clicked is immediate:
            try:
                self.file_queue().pause()
                if self._file_runner:
                    self._file_runner.interrupt()
                if not self._exit_ai_now():
                    event.ignore()
                    return False
                if self.all_items:
                    self._save_current_session(strict=True, stage='退出前保存任务状态')
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, '无法安全退出', f'关键任务状态未确认保存，请保持程序打开：{exc}')
                event.ignore()
                return False
            self._tasks_closing = True
            return True
        self._tasks_exit_pending = False
        if self._file_runner:
            self._file_runner.stop.clear()
        if self._ai_runner:
            self._ai_runner.stop_after_group.clear()
        event.ignore()
        return False

    def _maybe_exit_after_tasks(self):
        if self._tasks_exit_pending and not self.file_busy() and self._ai_runner is None:
            if any(t.get('persistence_error') for t in self.file_store().tasks.values()) or any(t.get('state') == 'save_failed' for t in self.ai_tasks):
                self._tasks_exit_pending = False
                return
            try:
                if self.all_items:
                    self._save_current_session(strict=True, stage='安全停止后退出')
            except OSError:
                self._tasks_exit_pending = False
                return
            self._tasks_closing = True
            QTimer.singleShot(0, self.close)
