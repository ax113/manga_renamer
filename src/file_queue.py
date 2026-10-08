"""Persistent FIFO scheduling; actual execution still owns the process-wide lock."""
from .session_store import save_session


class FileQueue:
    def __init__(self, store):
        self.store = store
        self.path = store.root / 'queue_state.json'
        self.paused = False
        self.active_id = ''
        if self.path.exists():
            import json
            try:
                state = json.loads(self.path.read_text(encoding='utf-8'))
                self.paused = bool(state.get('paused'))
                self.active_id = state.get('active_id', '')
            except (OSError, ValueError) as exc:
                store.errors.append('queue_state.json: ' + str(exc))
                self.paused = True
        # Never start disk operations just because the application reopened.
        if self.active_id or self.waiting():
            self.paused = True
        self.reset_if_idle()

    def control_task(self):
        active = self.store.tasks.get(self.active_id)
        if active and active['state'] not in {'completed', 'cancelled'}:
            return active
        waiting = self.waiting()
        return waiting[0] if waiting else None

    def reset_if_idle(self):
        if self.control_task() is None and not self.store.errors:
            self.active_id = ''
            self.paused = False

    def waiting(self):
        return sorted((t for t in self.store.tasks.values() if t['state']=='queued'),
                      key=lambda t: (t['created_at'], t['task_id']))

    def save(self):
        try:
            save_session(self.path, dict(paused=self.paused, active_id=self.active_id))
        except (OSError,ValueError):
            self.paused=True
            raise

    def submit(self, task):
        with self.store.lock:
            if self.store.errors: raise ValueError('任务记录读取异常，不能加入队列')
            self.reset_if_idle()
            task['state']='queued'
            self.store.save(task, 'task_queued')
            self.save()

    def start(self, task):
        with self.store.lock:
            if self.active_id and self.active_id != task['task_id']:
                raise ValueError('上一任务尚未完成，请先处理上一任务')
            self.active_id=task['task_id']; self.save()

    def finish(self, task):
        with self.store.lock:
            if self.active_id != task['task_id']: return
            if task['state']=='completed': self.active_id=''
            else: self.paused=True
            self.reset_if_idle()
            self.save()

    def pause(self):
        self.paused=True; self.save()

    def resume(self):
        with self.store.lock:
            if self.store.errors: raise ValueError('任务记录读取异常，不能继续队列')
            if self.active_id:
                active=self.store.tasks.get(self.active_id)
                if active and active['state'] not in {'completed','queued'}:
                    raise ValueError('请先继续 / 重试上一任务；上一任务完成后才能继续队列')
                self.active_id=''
            self.paused=False; self.save()

    def resume_for(self, task_id):
        with self.store.lock:
            if self.store.errors:
                raise ValueError('任务记录读取异常，不能继续队列')
            if self.active_id and self.active_id != task_id:
                active = self.store.tasks.get(self.active_id)
                if active and active['state'] not in {'completed', 'cancelled'}:
                    raise ValueError('请先处理当前暂停的任务')
            self.paused = False
            self.save()

    def next(self, library_id):
        if self.paused or self.active_id: return None
        waiting=self.waiting()
        if not waiting: return None
        task=waiting[0]
        if task['library_id']!=library_id:
            self.pause()
            return None
        return task

    def cancel(self, task_id):
        with self.store.lock:
            task=self.store.tasks[task_id]
            if task['state']!='queued' or self.active_id==task_id: raise ValueError('只能取消尚未开始的排队任务')
            task['state']='cancelled'
            for row in task['rows']:
                row.update(state='unexecuted', reason='用户取消排队，未执行文件操作')
            self.store.save(task, 'queue_cancelled')
            self.reset_if_idle()
            self.save()
