"""Durable, non-overwriting in-place rename/undo tasks, independent of Qt.

The persisted running intent precedes the syscall. Recovery compares filesystem
object identities at the two recorded paths; names alone never imply success.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
import copy
import ctypes
import errno
import json
import os
import stat
import struct
import sys
import threading
import uuid
import time
from .file_transfer import cross_volume_move, recover_transfer, transfer_published, TransferStopped

from .path_rules import path_key, target_problem
from .scanner import ARCHIVE_EXTENSIONS, strip_archive_suffix
from .file_operations import ACTIONS, move_target_problem, file_labels, result_status
from .session_store import save_session, item_to_dict, item_from_dict

ROW_LABELS = {'pending': '待执行', 'running': '执行中', 'success': '成功',
              'failed': '失败', 'blocked': '执行前阻断', 'unexecuted': '未执行',
              'uncertain': '待确认'}
TASK_LABELS = {'ready': '待执行', 'running': '执行中', 'completed': '已完成',
               'stopped': '已暂停', 'interrupted': '已中断', 'attention': '有项目需处理',
               'storage_unavailable': '存储不可访问', 'save_failed': '保存失败', 'queued': '排队等待', 'cancelled': '已取消排队'}
UNFINISHED = {'pending', 'unexecuted', 'running', 'uncertain', 'failed', 'blocked'}


def now():
    return datetime.now().isoformat(timespec='microseconds')


def _birth_ns(path, st):
    if hasattr(st, 'st_birthtime_ns'):
        return int(st.st_birthtime_ns)
    if os.name == 'nt':
        return int(st.st_ctime_ns)  # Creation time on older Windows Python versions.
    if sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        statx = getattr(libc, 'statx', None)
        if statx is not None:
            buffer = ctypes.create_string_buffer(256)
            statx.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_uint, ctypes.c_void_p]
            statx.restype = ctypes.c_int
            if statx(-100, os.fsencode(path), 0x100, 0x800, buffer) == 0:
                mask = struct.unpack_from('=I', buffer.raw, 0)[0]
                inode = struct.unpack_from('=Q', buffer.raw, 32)[0]
                if mask & 0x800 and inode == st.st_ino:
                    seconds, nanos = struct.unpack_from('=qI', buffer.raw, 80)
                    return seconds * 1000000000 + nanos
    return 0


def object_identity(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
        raise ValueError('符号链接 / 联接 / 重解析点不纳入真实文件操作')
    if stat.S_ISDIR(st.st_mode):
        kind = 'directory'
    elif stat.S_ISREG(st.st_mode) and Path(path).suffix.lower() in ARCHIVE_EXTENSIONS:
        kind = 'archive'
    else:
        raise ValueError('只支持漫画文件夹和 ZIP / RAR / 7Z / CBZ / CBR 压缩包')
    if not st.st_ino:
        raise ValueError('当前存储未提供可核验的文件身份，不能安全执行')
    return {'device': int(st.st_dev), 'inode': int(st.st_ino), 'kind': kind,
            'birth_ns': _birth_ns(path, st)}


def same_identity(path, expected):
    try:
        return bool(expected) and object_identity(path) == expected
    except (OSError, ValueError):
        return False


def identity_key(identity):
    return json.dumps(identity, sort_keys=True, separators=(',', ':'))


def exact_entry(path):
    """Case-sensitive existence also distinguishes a Windows case-only rename."""
    try:
        if not os.path.lexists(path):
            return False
        if os.name != 'nt':
            return True
        return os.path.basename(os.path.realpath(path)) == os.path.basename(path)
    except OSError:
        return False


def preflight(row, ai_ids=(), directories=None, *, allow_noop=False):
    source, target = row['source_path'], row['target_path']
    if row.get('steps'):
        return chain_preflight(row, ai_ids, directories)
    if transfer_published(row):
        if row.get('plan_problem'): return row['plan_problem']
        if row.get('local_id') in ai_ids: return '正在 AI 复核，不能继续清理'
        if not same_identity(target, row['transfer'].get('staging_identity')): return '完整目标身份已变化，请先核对'
        return ''
    virtual = row.get('_virtual_source', False)
    operation = row.get('operation', 'rename')
    problem = row.get('plan_problem') or (
        move_target_problem(source, target, row.get('identity', {}), row.get('target_parent_identity'), virtual=virtual)
        if operation in {'move', 'undo_move'} else target_problem(source, target))
    if problem:
        return problem
    if row.get('local_id') in ai_ids:
        return '正在 AI 复核，本漫画不能同时进入文件任务'
    if not virtual and (not same_identity(source, row.get('identity')) or not exact_entry(source)):
        return '源路径不存在或源对象身份 / 类型已变化；不会猜测其他对象'
    if source == target:
        # Display may classify a valid no-op separately; execution still rejects it.
        return '' if allow_noop else ('位置未变，无需移动' if operation in {'move', 'undo_move'} else '名称不变，无需执行')
    if row['identity']['kind'] == 'archive':
        if strip_archive_suffix(source)[1] != strip_archive_suffix(target)[1]:
            return '压缩包扩展名必须保持不变'
    # Apply Windows collisions even when tests run on a case-sensitive filesystem.
    try:
        if directories is not None:
            parent = os.path.dirname(target)
            if parent not in directories:
                with os.scandir(parent) as it:
                    indexed = defaultdict(list)
                    for entry in it:
                        indexed[path_key(entry.path)].append(entry.path)
                    directories[parent] = indexed
            collisions = directories[parent].get(path_key(target), [])
        elif os.name == 'nt':
            collisions = [target] if os.path.lexists(target) else []
        else:
            with os.scandir(os.path.dirname(target)) as it:
                collisions = [e.path for e in it if path_key(e.path) == path_key(target)]
        for existing in collisions:
            if path_key(existing) in row.get('_vacated', set()) and same_identity(existing, row['identity']):
                continue
            if path_key(source) == path_key(existing) and same_identity(existing, row['identity']):
                continue
            return '目标位置已存在同名文件 / 文件夹（包括仅大小写差异）'
    except OSError as exc:
        return f'父目录不可访问：{exc}'
    return ''


def chain_preflight(row, ai_ids=(), directories=None):
    if row.get('plan_problem'): return row['plan_problem']
    pending = [s for s in row['steps'] if s['state'] != 'success']
    if not pending: return '所有撤销步骤已完成'
    vacated = set()
    for index, step in enumerate(pending):
        candidate = dict(row)
        candidate.pop('steps', None)
        candidate.update(step, identity=step.get('identity', row.get('result_identity',row['identity'])), _virtual_source=index > 0, _vacated=set(vacated))
        problem = preflight(candidate, ai_ids, directories)
        if problem: return step['label'] + '：' + problem
        vacated.add(path_key(step['source_path']))
    return ''


def no_replace_rename(source, target):
    """Use an atomic no-replace syscall; never fall back to POSIX overwrite."""
    if os.name == 'nt':
        os.rename(source, target)  # Windows os.rename never replaces an existing target.
    elif sys.platform.startswith('linux'):
        libc = ctypes.CDLL(None, use_errno=True)
        rename = getattr(libc, 'renameat2', None)
        if rename is None:
            raise OSError(errno.ENOTSUP, '系统缺少安全的不覆盖改名接口')
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        rename.restype = ctypes.c_int
        if rename(-100, os.fsencode(source), -100, os.fsencode(target), 1):
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), source)
    else:
        raise OSError(errno.ENOTSUP, '当前系统不支持本版安全改名接口')


def counts(task):
    return Counter(row['state'] for row in task.get('rows', []))


def validate_steps(row):
    steps = row.get('steps')
    if steps is None: return
    if not isinstance(steps, list) or not steps: raise ValueError('撤销步骤记录为空')
    previous = row['source_path']
    for step in steps:
        if not isinstance(step, dict) or step.get('state') not in ROW_LABELS or step.get('operation') not in {'undo', 'undo_move'}:
            raise ValueError('撤销步骤类型 / 状态无法核对')
        if step.get('source_path') != previous or not all(isinstance(step.get(k), str) and step[k] for k in ('target_path', 'reverses_task_id', 'reverses_row', 'label')):
            raise ValueError('撤销步骤路径 / 关联记录不完整')
        previous = step['target_path']
    if previous != row['target_path']: raise ValueError('撤销步骤最终路径不一致')


def validate_operation(row, action):
    if row.get('operation', action) != action or '_virtual_source' in row or '_vacated' in row:
        raise ValueError('项目操作类型与任务不一致')
    validate_steps(row)
    if row.get('steps') and action not in {'undo', 'undo_move'}:
        raise ValueError('只有撤销任务可以包含关联步骤')


def recover_operation(entry, identity):
    source, target = entry['source_path'], entry['target_path']
    old = exact_entry(source) and same_identity(source, identity)
    new = exact_entry(target) and same_identity(target, identity)
    if new and not old:
        entry.update(state='success', completed_at=now(), reason='恢复核验：本步骤已完成')
    elif old and not new and (not os.path.lexists(target) or path_key(source) == path_key(target)):
        entry.update(state='unexecuted', reason='恢复核验：本步骤尚未完成')
    else:
        entry.update(state='uncertain', reason='无法确认本步骤磁盘结果')


class FileTaskStore:
    def __init__(self, data_root, log_root=None):
        self.root = Path(data_root) / 'file_tasks'
        self.root.mkdir(parents=True, exist_ok=True)
        self.log_root = Path(log_root or Path(data_root) / 'logs') / 'file_tasks'
        self.tasks, self.errors = {}, []
        self.lock = threading.RLock()
        self._execution_fd = None
        self._baseline = {}
        baseline = self.root / 'retained_state.json'
        if baseline.exists():
            try:
                self._baseline = json.loads(baseline.read_text(encoding='utf-8')).get('receipts', {})
            except (OSError, ValueError) as exc:
                self.errors.append(f'{baseline.name}: {exc}')
        for p in sorted(self.root.glob('F-*.json')):
            try:
                task = json.loads(p.read_text(encoding='utf-8'))
                if task.get('file_task_format') not in {1, 2, 3} or not isinstance(task.get('rows'), list):
                    raise ValueError('任务格式不支持')
                if p.stem != task['task_id'] or task['task_id'] in self.tasks:
                    raise ValueError('任务编号不一致')
                if not isinstance(task.get('library_id'), str) or not task['library_id']:
                    raise ValueError('任务漫画库身份缺失')
                for row in task['rows']:
                    if not isinstance(row, dict) or row.get('state') not in ROW_LABELS:
                        raise ValueError('项目状态无法核对')
                    if not all(isinstance(row.get(k), str) and row[k] for k in ('local_id', 'source_path', 'target_path')):
                        raise ValueError('项目身份 / 路径记录缺失')
                    if not isinstance(row.get('identity'), dict) or row['identity'].get('kind') not in {'directory', 'archive'}:
                        raise ValueError('项目磁盘身份记录缺失')
                if task.get('action') not in ACTIONS:
                    raise ValueError('文件操作类型无法核对')
                for row in task['rows']:
                    validate_operation(row, task['action'])
                journal = self.root / (task['task_id'] + '.journal')
                if journal.exists():
                    lines = journal.read_text(encoding='utf-8').splitlines()
                    sequence = int(task.get('commit_seq', 0))
                    for index, line in enumerate(lines):
                        try:
                            patch = json.loads(line)
                        except ValueError:
                            if index == len(lines) - 1:
                                task['journal_warning'] = '最后一条持久记录不完整，已保留此前记录，需重新核对'
                                break
                            raise ValueError('任务逐项记录中间损坏')
                        if patch['seq'] <= sequence:
                            continue
                        if patch['seq'] != int(task.get('commit_seq', 0)) + 1:
                            raise ValueError('任务逐项记录序号中断')
                        task['rows'][patch['index']] = patch['row']
                        task.update(commit_seq=patch['seq'], updated_at=patch['at'], state=patch['state'])
                        task.setdefault('events', []).append(patch['event'])
                for row in task['rows']:
                    validate_operation(row, task['action'])
                self.tasks[task['task_id']] = task
            except (OSError, ValueError, KeyError) as exc:
                self.errors.append(f'{p.name}: {exc}')

    def list(self):
        return sorted(self.tasks.values(), key=lambda t: t['created_at'], reverse=True)

    def save(self, task, event='', **details):
        with self.lock:
            task['updated_at'] = now()
            task['commit_seq'] = int(task.get('commit_seq', 0)) + 1
            if event:
                task.setdefault('events', []).append({'at': task['updated_at'], 'event': event, **details})
            journal = self.root / (task['task_id'] + '.journal')
            if event in {'item_intent', 'item_result', 'item_blocked'}:
                index = next(i for i, r in enumerate(task['rows']) if r['local_id'] == details['local_id'])
                patch = {'seq': task['commit_seq'], 'index': index, 'row': task['rows'][index],
                         'at': task['updated_at'], 'state': task['state'], 'event': task['events'][-1]}
                with journal.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(patch, ensure_ascii=False, separators=(',', ':')) + '\n')
                    stream.flush()
                    os.fsync(stream.fileno())
            else:
                # Batch checkpoints fold the small per-item write-ahead journal.
                save_session(self.root / (task['task_id'] + '.json'), task)
                try:
                    journal.unlink(missing_ok=True)
                except OSError:
                    pass  # Old sequences are ignored on recovery.
            if event:
                try:
                    self.log_root.mkdir(parents=True, exist_ok=True)
                    with (self.log_root / (task['task_id'] + '.jsonl')).open('a', encoding='utf-8') as stream:
                        stream.write(json.dumps(task['events'][-1], ensure_ascii=False) + '\n')
                        stream.flush()
                        os.fsync(stream.fileno())
                except OSError as exc:
                    task['log_warning'] = str(exc)  # The durable primary task still owns all events.

    def acquire_execution(self):
        if self.errors:
            raise ValueError('有任务记录无法读取，请保留文件并先处理：' + '; '.join(self.errors))
        if self._execution_fd is not None:
            raise ValueError('已有文件任务执行中')
        fd = (self.root / 'execution.lock').open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                fd.seek(0); fd.write(b'0'); fd.flush(); fd.seek(0)
                msvcrt.locking(fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fd.close()
            raise ValueError('另一个工具实例正在执行文件任务')
        self._execution_fd = fd

    def release_execution(self):
        if self._execution_fd is not None:
            self._execution_fd.close()
            self._execution_fd = None

    def create(self, library_id, session_id, directory, rows, action='rename', parent_id=''):
        if action not in ACTIONS:
            raise ValueError('文件操作类型不支持')
        if not library_id:
            raise ValueError('没有已加载的漫画库身份')
        if len({r['local_id'] for r in rows}) != len(rows):
            raise ValueError('漫画内部编号重复，不能创建文件任务')
        task = {'file_task_format': 3, 'task_id': 'F-' + uuid.uuid4().hex,
                'library_id': library_id, 'session_id': session_id,
                'work_directory': str(directory), 'created_at': now(), 'state': 'ready',
                'action': action, 'parent_task_id': parent_id, 'rows': copy.deepcopy(rows), 'events': []}
        groups = defaultdict(list)
        receipts=self.receipts(library_id)
        for row in task['rows']:
            row.update(state='pending', attempts=0, reason='', history=[])
            row['operation'] = action
            if not row.get('tracking_identity'):
                receipt=next((r for r in receipts.values() if r['identity']==row['identity']),{})
                row['tracking_identity']=copy.deepcopy(receipt.get('tracking_identity',row['identity']))
            validate_operation(row, action)
            groups[path_key(row['target_path'])].append(row)
        for group in groups.values():
            if len(group) > 1:
                for row in group:
                    row['plan_problem'] = f'本批有 {len(group)} 项最终目标相同'
        self.save(task, 'task_created', total=len(rows))
        self.tasks[task['task_id']] = task
        return task

    def recover(self, library_id, *, recheck_blocked=False):
        """Only loaded-library recovery. Historical tasks stay read-only."""
        self.acquire_execution()
        try:
            self._recover_locked(library_id, recheck_blocked=recheck_blocked)
        finally:
            self.release_execution()

    def _recover_locked(self, library_id, *, recheck_blocked=False):
        for task in self.list():
            if task['library_id'] != library_id:
                continue
            if task['state'] in {'queued','cancelled'}: continue
            changed = False
            for row in task['rows']:
                if recheck_blocked and row['state'] == 'blocked' and not preflight(row):
                    row.update(state='unexecuted', reason='重新核对通过，可继续尚未完成的操作')
                    for step in row.get('steps', []):
                        if step['state'] == 'blocked':
                            step.update(state='unexecuted', reason='')
                    changed = True
                    continue
                if row['state'] not in {'running', 'uncertain'}:
                    continue
                if row.get('steps'):
                    for step in row['steps']:
                        if step['state'] in {'running', 'uncertain'}:
                            identity=step.get('identity',row['identity'])
                            if step.get('transfer'): recover_transfer(step,identity)
                            else: recover_operation(step,identity)
                            if step['state']=='success': row['result_identity']=step.get('result_identity',identity)
                    if all(s['state'] == 'success' for s in row['steps']):
                        row.update(state='success', reason='中断恢复：全部撤销步骤已核验', completed_at=row['steps'][-1].get('completed_at', now()))
                    elif any(s['state'] == 'uncertain' for s in row['steps']):
                        row.update(state='uncertain', reason='撤销步骤磁盘结果待确认，请重新核对')
                    else:
                        row.update(state='unexecuted', reason='前面步骤保留，继续处理尚未完成的撤销步骤')
                    changed = True
                    continue
                if row.get('transfer'):
                    recover_transfer(row,row['identity'])
                    changed=True
                    continue
                source, target = row['source_path'], row['target_path']
                old = exact_entry(source) and same_identity(source, row['identity'])
                new = exact_entry(target) and same_identity(target, row['identity'])
                if new and not old:
                    row.update(state='success', reason='中断恢复：已核验记录目标的磁盘身份', completed_at=now())
                elif old and not new and not os.path.lexists(target):
                    row.update(state='unexecuted', reason='中断恢复：原对象仍在源路径，尚未改名')
                elif old and not new and path_key(source) == path_key(target):
                    row.update(state='unexecuted', reason='中断恢复：原大小写名称未改变')
                else:
                    row.update(state='uncertain', reason='无法根据两条记录路径确认对象；请核对后点击“重新核对”')
                changed = True
            if task['state'] == 'running':
                task['state'] = 'interrupted'
                for row in task['rows']:
                    if row['state'] == 'pending':
                        row['state'] = 'unexecuted'
                changed = True
            if changed:
                task.pop('persistence_error', None)
                task.pop('journal_warning', None)
                if all(r['state'] == 'success' for r in task['rows']):
                    task['state'] = 'completed'
                self.save(task, 'recovered')

    def receipts(self, library_id):
        result = copy.deepcopy(self._baseline.get(library_id, {}))
        legacy = {key for key, receipt in result.items() if 'operations' not in receipt}

        def advance(operations, task, row, entry):
            operation = entry.get('operation', task['action'])
            if operation in {'rename', 'move'}:
                operations.append({'task_id': task['task_id'], 'row_id': row['local_id'],
                    'action': operation, 'source_path': entry['source_path'], 'target_path': entry['target_path'],
                    'identity': row['identity']})
                return operations
            reverse_id = entry.get('reverses_task_id', task.get('parent_task_id'))
            reverse_row = entry.get('reverses_row', row.get('parent_row', row['local_id']))
            return [op for op in operations if (op['task_id'], op['row_id']) != (reverse_id, reverse_row)]

        def adopt_legacy(key):
            receipt = result[key]
            known = {'已改名' if op['action'] == 'rename' else '已移动' for op in receipt.get('operations', [])}
            # A cleaned fix5 task retained its tag but did not retain operation references.
            receipt['retained_labels'] = sorted(set(file_labels(receipt.get('status'))) - known)
            receipt.setdefault('operations', [])
            legacy.discard(key)

        effects = []
        for task in self.list():
            if task['library_id'] != library_id: continue
            for row in task['rows']:
                entries = row.get('steps') or [row]
                for index, entry in enumerate(entries):
                    if entry['state'] not in {'success', 'failed'}: continue
                    effects.append((entry.get('completed_at', task['updated_at']), task, row, entry, index))
        for stamp, task, row, entry, index in sorted(effects, key=lambda e: e[0]):
            key = identity_key(row.get('tracking_identity',row['identity']))
            previous = result.get(key, {})
            if previous.get('at', '') >= stamp:
                if key in legacy and entry['state'] == 'success':
                    previous['operations'] = advance(previous.get('operations', []), task, row, entry)
                continue
            if key in legacy: adopt_legacy(key)
            operations = copy.deepcopy(previous.get('operations', []))
            operation = entry.get('operation', task['action'])
            success = entry['state'] == 'success'
            if success:
                operations = advance(operations, task, row, entry)
                labels = {'已改名' if op['action'] == 'rename' else '已移动' for op in operations} | set(previous.get('retained_labels', []))
                status = entry.get('result_status') or result_status(labels, operation)
            else:
                status = previous.get('status') or entry.get('previous_status', row.get('previous_status', '未执行'))
                if operation in {'rename', 'move'} and not file_labels(status): status = '执行失败'
            result[key] = {'at': stamp, 'path': entry['target_path'] if success else entry['source_path'],
                'identity': entry.get('result_identity',entry.get('identity',row['identity'])) if success else entry.get('identity',row['identity']), 'tracking_identity': row.get('tracking_identity',row['identity']), 'status': status, 'operations': operations,
                'retained_labels': previous.get('retained_labels', []),
                'local_id': row['local_id'], 'session_id': row.get('session_id', task['session_id']),
                'snapshot': row.get('item_snapshot', {}), 'source_path': row['source_path']}
        for key in list(legacy): adopt_legacy(key)
        return result

    def undone_rows(self, task_id):
        result = set()
        original = self.tasks.get(task_id)
        if not original: return result
        for task in self.list():
            if task['library_id'] != original['library_id']: continue
            for row in task['rows']:
                for entry in row.get('steps') or [row]:
                    if entry['state'] != 'success': continue
                    if entry.get('reverses_task_id', task.get('parent_task_id')) == task_id:
                        result.add(entry.get('reverses_row', row.get('parent_row', row['local_id'])))
        return result

    def reconcile_items(self, items, library_id, session_id, restore=False, snapshots=None):
        receipts = self.receipts(library_id)
        for index, item in enumerate(items):
            identity = getattr(item, 'file_identity', {})
            tracking = item.extra.get('file_tracking_identity',identity)
            receipt = receipts.get(identity_key(tracking)) if tracking else None
            if not receipt and identity:
                receipt=next((r for r in receipts.values() if r['identity']==identity),None)
            if restore and not receipt:
                receipt = next((r for r in receipts.values() if r['session_id'] == session_id and r['local_id'] == item.local_id), None)
            try:
                if not receipt:
                    identity = object_identity(item.original_path)
                    receipt = next((r for r in receipts.values() if r['identity']==identity),None)
                if receipt and same_identity(receipt['path'], receipt['identity']):
                    if restore or path_key(item.original_path) == path_key(receipt['path']):
                        if not restore:
                            current = (snapshots or {}).get(identity_key(receipt['identity']))
                            if current:
                                old = item_from_dict(current)
                                old.local_id = item.local_id
                                old.checked = False
                                items[index] = item = old
                            elif receipt.get('snapshot'):
                                # Static task inputs may predate later manual/AI work. Only
                                # recover the trusted gallery link, never revive those old states.
                                old = item_from_dict(receipt['snapshot'])
                                if old.record is not None:
                                    item.record = old.record
                                    item.match_method = '文件任务身份关联'
                                    item.warning = ''
                        item.original_path = receipt['path']
                        stem, suffix = strip_archive_suffix(Path(receipt['path']).name) if receipt['identity']['kind'] == 'archive' else (Path(receipt['path']).name, '')
                        item.original_name, item.suffix = stem, suffix
                        item.file_status, identity = receipt['status'], receipt['identity']
                        item.extra['file_tracking_identity']=copy.deepcopy(receipt.get('tracking_identity',receipt['identity']))
                item.file_identity = identity or object_identity(item.original_path)
            except (OSError, ValueError):
                pass  # Missing objects never turn into guessed matches.

    def clean(self, task_id):
        self.acquire_execution()
        try:
            self._clean_locked(task_id)
        finally:
            self.release_execution()

    def _clean_locked(self, task_id):
        task = self.tasks[task_id]
        if task['state'] == 'running' or any(r['state'] in UNFINISHED for r in task['rows']):
            raise ValueError('只能清理没有失败、未执行、阻断或待确认项的已完成记录')
        for other in self.tasks.values():
            if other is task: continue
            if any(s.get('reverses_task_id') == task_id for row in other['rows'] for s in row.get('steps', [])) and any(row['state'] in UNFINISHED for row in other['rows']):
                raise ValueError('未完成撤销任务仍引用本记录，请先处理该任务')
        library_id = task['library_id']
        self._baseline[library_id] = self.receipts(library_id)
        save_session(self.root / 'retained_state.json', {'receipts': self._baseline})
        (self.root / (task_id + '.json')).unlink()
        del self.tasks[task_id]


class FileTaskRunner(threading.Thread):
    def __init__(self, store, task, selected_states=('pending', 'unexecuted'), ai_ids=(),
                 result=None, done=None, rename=None, progress=None):
        super().__init__(daemon=True)
        self.store, self.task = store, task
        self.selected_states, self.ai_ids = set(selected_states), set(ai_ids)
        self.result, self.done = result or (lambda *_: None), done or (lambda *_: None)
        self.rename = rename or no_replace_rename
        self.progress = progress or (lambda *_: None)
        self._last_progress=0.0
        self.stop = threading.Event()
        self.abandon = threading.Event()

    def interrupt(self):
        self.stop.set()
        with self.store.lock:
            self.task['state'] = 'interrupted'
            for row in self.task['rows']:
                if row['state'] == 'running':
                    row.update(state='uncertain', reason='立即退出时文件操作结果未确认')
                    for step in row.get('steps', []):
                        if step['state'] == 'running': step['state'] = 'uncertain'
                elif row['state'] == 'pending':
                    row['state'] = 'unexecuted'
            self.store.save(self.task, 'immediate_exit')
            self.abandon.set()

    def storage_available(self):
        try:
            with os.scandir(self.task['work_directory']):
                return True
        except OSError:
            return False

    def _execute(self, row, entry):
        identity=entry.get('identity',row['identity'])
        entry.setdefault('identity',copy.deepcopy(identity))
        cross=entry.get('transfer') or (entry.get('operation',self.task['action']) in {'move','undo_move'} and os.stat(os.path.dirname(entry['target_path'])).st_dev != identity['device'])
        def report(phase, done, total):
            moment=time.monotonic()
            if moment-self._last_progress<.15 and phase==getattr(self,'_progress_phase','') and done!=total: return
            self._last_progress=moment; self._progress_phase=phase
            self.progress(self.task['task_id'],dict(local_id=row['local_id'],name=Path(entry['source_path']).name,phase=phase,bytes_done=done,bytes_total=total))
        if cross:
            def checkpoint():
                with self.store.lock:
                    if self.abandon.is_set(): raise TransferStopped('程序退出，等待恢复核验')
                    self.store.save(self.task,'item_intent',local_id=row['local_id'],phase=entry['transfer']['phase'])
            result=cross_volume_move(entry,identity,checkpoint,self.stop,report,self.rename)
        else:
            report('moving',0,0)
            self.rename(entry['source_path'],entry['target_path'])
            if not same_identity(entry['target_path'],identity): raise RuntimeError('文件操作后的磁盘身份无法确认')
            result=identity
            report('done',0,0)
        entry['result_identity']=copy.deepcopy(result)
        row['result_identity']=copy.deepcopy(result)

    def _error_state(self, entry, error):
        if isinstance(error,TransferStopped): return 'unexecuted'
        if entry.get('transfer'):
            if transfer_published(entry): return 'failed'
            if same_identity(entry['source_path'],entry.get('identity')): return 'failed'
            return 'uncertain'
        if not same_identity(entry['source_path'],entry.get('identity')): return 'uncertain'
        return 'blocked' if isinstance(error,ValueError) else 'failed'

    def _run_steps(self, row):
        for step in row['steps']:
            if step['state'] == 'success': continue
            if self.stop.is_set() or self.abandon.is_set():
                row.update(state='unexecuted', reason='已完成步骤保留，剩余步骤等待继续')
                return
            step.setdefault('identity',copy.deepcopy(row.get('result_identity',row['identity'])))
            candidate = dict(row); candidate.pop('steps', None); candidate.pop('transfer',None); candidate.update(step)
            problem = preflight(candidate, self.ai_ids)
            if problem:
                row.update(state='blocked', reason=step['label'] + '：' + problem)
                step.update(state='blocked', reason=problem)
                self.store.save(self.task, 'item_blocked', local_id=row['local_id'], reason=row['reason'])
                return
            with self.store.lock:
                if self.abandon.is_set(): return
                row.update(state='running', reason='')
                step.update(state='running', reason='')
                self.store.save(self.task, 'item_intent', local_id=row['local_id'],
                    step=step['label'], source=step['source_path'], target=step['target_path'])
            error = None
            for attempt in range(3):
                row['attempts'] += 1
                step['attempts'] = step.get('attempts', 0) + 1
                try:
                    problem = preflight(candidate, self.ai_ids)
                    if problem: raise ValueError(problem)
                    self._execute(row,step)
                    error = None
                    break
                except (OSError, ValueError, RuntimeError) as exc:
                    if transfer_published(step) and not isinstance(exc,TransferStopped): exc=RuntimeError('目标已完整复制；源清理待完成：'+str(exc))
                    error = exc
                    transient = isinstance(exc, OSError) and (getattr(exc, 'winerror', None) in {5,32,33} or exc.errno in {errno.EACCES,errno.EPERM,errno.EBUSY})
                    if not transient or attempt == 2 or self.stop.wait(.08*(attempt+1)): break
            with self.store.lock:
                if self.abandon.is_set(): return
                if error:
                    state = self._error_state(step,error)
                    step.update(state=state, reason=str(error), completed_at=now())
                    row.update(state=state, reason=step['label']+'失败，后续步骤未执行：'+str(error))
                    row['history'].append({'at':now(),'state':state,'reason':row['reason']})
                else:
                    step.update(state='success', reason='', completed_at=now())
                    if all(s['state']=='success' for s in row['steps']):
                        row.update(state='success', reason='', completed_at=step['completed_at'])
                    row['history'].append({'at':step['completed_at'],'state':'success','reason':step['label']+'完成'})
                self.store.save(self.task,'item_result',local_id=row['local_id'],state=row['state'],reason=row['reason'])
            self.result(self.task['task_id'],copy.deepcopy(row))
            if error: return

    def run(self):
        task = self.task
        awaiting_commit = None
        try:
            with self.store.lock:
                if self.abandon.is_set():
                    return
                task['state'] = 'running'
                task.pop('persistence_error', None)
                self.store.save(task, 'task_started')
            for row in task['rows']:
                if row['state'] not in self.selected_states:
                    continue
                if self.stop.is_set() or self.abandon.is_set():
                    break
                if not self.storage_available():
                    task['state'] = 'storage_unavailable'
                    break
                problem = preflight(row, self.ai_ids)
                if problem:
                    row.update(state='blocked', reason=problem)
                    self.store.save(task, 'item_blocked', local_id=row['local_id'], reason=problem)
                    self.result(task['task_id'], copy.deepcopy(row))
                    continue
                if row.get('steps'):
                    awaiting_commit = row
                    self._run_steps(row)
                    awaiting_commit = None
                    self.result(task['task_id'], copy.deepcopy(row))
                    if row['state'] == 'uncertain':
                        task['state'] = 'attention'
                        break
                    continue
                with self.store.lock:
                    if self.abandon.is_set():
                        break
                    row.update(state='running', reason='')
                    self.store.save(task, 'item_intent', local_id=row['local_id'],
                                    source=row['source_path'], target=row['target_path'])
                    awaiting_commit = row
                error = None
                for attempt in range(3):
                    row['attempts'] += 1
                    try:
                        # Revalidate before every attempt, including after transient locks.
                        problem = preflight(row, self.ai_ids)
                        if problem:
                            raise ValueError(problem)
                        self._execute(row,row)
                        error = None
                        break
                    except (OSError, ValueError, RuntimeError) as exc:
                        if transfer_published(row) and not isinstance(exc,TransferStopped): exc=RuntimeError('目标已完整复制；源清理待完成：'+str(exc))
                        error = exc
                        transient = isinstance(exc, OSError) and (getattr(exc, 'winerror', None) in {5, 32, 33} or exc.errno in {errno.EACCES, errno.EPERM, errno.EBUSY})
                        if not transient or attempt == 2 or self.stop.wait(.08 * (attempt + 1)):
                            break
                with self.store.lock:
                    if self.abandon.is_set():
                        break
                    if error:
                        row.update(state=self._error_state(row,error), reason=str(error), completed_at=now())
                        row['history'].append({'at': now(), 'state': row['state'], 'reason': str(error)})
                    else:
                        row.update(state='success', reason='', completed_at=now())
                        row['history'].append({'at': now(), 'state': 'success'})
                    self.store.save(task, 'item_result', local_id=row['local_id'], state=row['state'], reason=row['reason'])
                    awaiting_commit = None
                self.result(task['task_id'], copy.deepcopy(row))
                if row['state'] == 'uncertain':
                    task['state'] = 'attention'
                    break
                if not self.storage_available():
                    task['state'] = 'storage_unavailable'
                    break
            if not self.abandon.is_set():
                for row in task['rows']:
                    if row['state'] == 'pending':
                        row['state'] = 'unexecuted'
                c = counts(task)
                if task['state'] == 'running':
                    task['state'] = 'stopped' if self.stop.is_set() and c['unexecuted'] else ('attention' if c['failed'] or c['blocked'] or c['uncertain'] or c['unexecuted'] else 'completed')
                self.store.save(task, 'task_finished', state=task['state'], counts=dict(c))
        except Exception as exc:
            task['state'], task['persistence_error'] = 'save_failed', str(exc)
            if awaiting_commit is not None:
                awaiting_commit.update(state='uncertain', reason='磁盘操作已尝试，但结果未确认保存；请重新核对')
            for row in task['rows']:
                if row['state'] == 'running':
                    row.update(state='uncertain', reason='关键状态未确认保存；需重新核对磁盘')
            try:
                self.store.save(task, 'save_failed', error=str(exc))
            except OSError:
                pass
        finally:
            self.store.release_execution()
            self.done(task['task_id'])
