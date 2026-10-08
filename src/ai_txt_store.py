"""Persistent TXT workflow, with exact original-session lookup and atomic commits.

Current sessions use the existing save callback. Inactive sessions commit their
items and task records together to one permanent session file; an index is never
a substitute for the authoritative comic data.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .ai_supplements import current_input_fingerprint
from .ai_review import (business_input, fingerprint, input_item, validate_results,
                        apply_review_output)
from .ai_txt import (MAX_RESULT_BYTES, atomic_text, input_text, parse_result,
                     split_sizes, task_state, validate_meta)
from .models import CATEGORY_AI_REVIEWED, CATEGORY_LLM_REVIEW
from .session_store import item_from_dict, item_to_dict, load_session, save_session
from .run_log import _redact


def is_txt(task):
    return isinstance(task, dict) and task.get("transport") == "TXT"


def check_task_record(task):
    try:
        if not isinstance(task["task_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task["task_id"]):
            raise ValueError
        if not isinstance(task["created_at"], str) or not isinstance(task["config"]["model"], str):
            raise ValueError
        if not isinstance(task["items"], dict) or not task["items"] or not isinstance(task["parts"], list) or not task["parts"]:
            raise ValueError
        for local_id, row in task["items"].items():
            entry, state = row["input"], row["state"]
            ctrl = entry["CONTROL"]
            if ctrl["task_id"] != task["task_id"] or ctrl["local_id"] != local_id or type(ctrl["review_round"]) is not int or ctrl["review_round"] < 1:
                raise ValueError
            if not isinstance(entry["LOCAL"]["local_name"], str) or not isinstance(entry["STATE"]["review_reasons"], list) or not isinstance(ctrl["input_fingerprint"], str):
                raise ValueError
            if state not in {"pending", "export_pending", "success", "failed", "superseded", "missing"}:
                raise ValueError
        part_ids = set()
        for part in task["parts"]:
            if not isinstance(part["part_id"], str) or part["part_id"] in part_ids or not isinstance(part["ids"], list) or not part["ids"]:
                raise ValueError
            part_ids.add(part["part_id"])
            if len(set(part["ids"])) != len(part["ids"]) or any(i not in task["items"] for i in part["ids"]):
                raise ValueError
            if type(part["number"]) is not int or not isinstance(part["file"], str) or not isinstance(part["imports"], list) or type(part["supplement"]) is not bool:
                raise ValueError
            if part["state"] not in {"export_pending", "exported", "cancelled"}:
                raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("原TXT任务记录不完整，无法安全核对；请保留RESULT并恢复原会话记录") from None


class TxtSession:
    def __init__(self, payload, items=None, commit=None):
        self.payload = payload
        self.tasks = payload.setdefault("ai_tasks", [])
        if not isinstance(self.tasks, list):
            raise ValueError("原会话任务记录无法核对")
        for task in self.tasks:
            if is_txt(task):
                check_task_record(task)
                if task.get("session_id") != payload.get("session_id"):
                    raise ValueError("TXT任务与原会话身份不一致，无法安全核对")
        raw = payload.get("items", [])
        self.items = items if items is not None else [item_from_dict(x) for x in raw if isinstance(x, dict)]
        if len({x.local_id for x in self.items}) != len(self.items):
            raise ValueError("原会话漫画编号重复，无法安全核对")
        self.by_id = {x.local_id: x for x in self.items}
        self._commit = commit

    def backup(self):
        return copy.deepcopy(self.tasks), [copy.deepcopy(i.__dict__) for i in self.items]

    def rollback(self, backup):
        tasks, items = backup
        # Preserve live API/task references when a write fails.
        for i, state in zip(self.items, items):
            i.__dict__.clear(); i.__dict__.update(state)
        for task, original in zip(self.tasks, tasks):
            task.clear(); task.update(original)
        self.tasks[:] = self.tasks[:len(tasks)]

    def save(self):
        if self._commit is None:
            raise OSError("没有可靠的会话保存入口")
        self._commit(self)


class TxtRepository:
    def __init__(self, sessions_dir):
        self.root = Path(sessions_dir)
        self._files = {}
        self._sessions = {}
        self._tasks = {}
        self.warnings = []

    def scan(self):
        self.warnings = []
        paths = [self.root / "current_session.json"]
        for directory in ("archive", "checkpoints", "txt_sessions"):
            folder = self.root / directory
            if folder.is_dir():
                paths.extend(folder.glob("*.json"))
        candidates = defaultdict(list)
        for path in paths:
            try:
                stat = path.stat()
                signature = (stat.st_mtime_ns, stat.st_size)
                cached = self._files.get(str(path))
                if cached and cached[0] == signature:
                    data = cached[1]
                else:
                    data = load_session(path)
                    self._files[str(path)] = signature, data
                if not data or not data.get("session_id"):
                    continue
                candidates[data["session_id"]].append((int(data.get("session_revision", 0)), stat.st_mtime_ns, path, data))
            except FileNotFoundError:
                cached = self._files.get(str(path))
                if cached and cached[1] and cached[1].get("session_id"):
                    data = cached[1]
                    candidates[data["session_id"]].append((int(data.get("session_revision", 0)), cached[0][0], path, data))
                continue
            except (OSError, ValueError, TypeError) as exc:
                self.warnings.append(_redact(f"原会话记录暂不可读：{path.name}（{exc}）"))
                cached = self._files.get(str(path))
                if cached and cached[1] and cached[1].get("session_id"):
                    data = cached[1]
                    candidates[data["session_id"]].append((int(data.get("session_revision", 0)), cached[0][0], path, data))
        self._sessions = {sid: max(rows, key=lambda r: (r[0], r[1])) for sid, rows in candidates.items()}
        self._tasks = {}
        for sid, (_, _, path, payload) in self._sessions.items():
            raw_tasks = payload.get("ai_tasks", [])
            for task in raw_tasks if isinstance(raw_tasks, list) else []:
                if isinstance(task,dict) and task.get("session_id") == sid:
                    try:
                        if is_txt(task):
                            check_task_record(task)
                        elif (not isinstance(task.get("task_id"),str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}",task["task_id"]) or not isinstance(task.get("items"),dict)):
                            raise ValueError("原API任务记录不完整，无法显示历史")
                    except ValueError as exc:
                        self.warnings.append(str(exc))
                        continue
                    tid = task.get("task_id")
                    if tid in self._tasks:
                        self.warnings.append("任务编号对应多个原会话，已拒绝自动匹配")
                        self._tasks[tid] = None
                    else:
                        self._tasks[tid] = (sid, path, task)

    def tasks(self, active_tasks=()):
        tasks = {tid: row[2] for tid, row in self._tasks.items() if row is not None}
        tasks.update({t["task_id"]: t for t in active_tasks})
        return sorted(tasks.values(), key=lambda t: t.get("created_at", ""))

    def context(self, task_id):
        row = self._tasks.get(task_id)
        if row is None:
            raise ValueError("找不到可验证的原TXT任务，保留原文件后重试")
        sid, path, _ = row
        payload = load_session(path)  # Fresh bytes, never cached facts for application.
        if not payload or payload.get("session_id") != sid:
            raise ValueError("原会话身份无法核对")
        return TxtSession(payload, commit=self.save_inactive)

    def library_source(self, task_id):
        """Cached identity fields for display; never an action's source of truth."""
        row = self._tasks.get(task_id)
        if row is None:
            return None
        session = self._sessions.get(row[0])
        if session is None:
            return None
        payload = session[3]
        return {key: str(payload.get(key) or '') for key in
                ('library_id', 'source_database', 'work_directory')}

    def save_inactive(self, context):
        sid = context.payload["session_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            raise ValueError("原会话编号无效")
        payload = {**context.payload, "items": [item_to_dict(i) for i in context.items]}
        payload["session_revision"] = max(time.time_ns(), int(payload.get("session_revision", 0)) + 1)
        save_session(self.root / "txt_sessions" / (sid + ".json"), payload)
        context.payload.update(payload)

    def latest(self, payload):
        """A non-foreground RESULT may have advanced this exact session."""
        if not payload or not payload.get("session_id"):
            return payload
        sid = payload["session_id"]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", sid):
            return payload
        newer = load_session(self.root / "txt_sessions" / (sid + ".json"))
        if newer and newer.get("session_id") == sid and int(newer.get("session_revision", 0)) > int(payload.get("session_revision", 0)):
            return newer
        return payload

    def preserve_review_history(self, payload):
        """A checkpoint can restore edits, but cannot rewind sent TXT identities."""
        known = self._sessions.get(payload.get("session_id"))
        if not known or known[0] <= int(payload.get("session_revision", 0)):
            return payload
        latest = known[3]
        if not any(is_txt(t) for t in latest.get("ai_tasks", []) + payload.get("ai_tasks", [])):
            return payload
        recovered = copy.deepcopy(payload)
        tasks = {t["task_id"]: t for t in recovered.get("ai_tasks", [])}
        tasks.update({t["task_id"]: copy.deepcopy(t) for t in latest.get("ai_tasks", [])})
        recovered["ai_tasks"] = list(tasks.values())
        current = {i["local_id"]: i for i in latest.get("items", []) if isinstance(i, dict) and i.get("local_id")}
        for item in recovered.get("items", []):
            original = current.get(item.get("local_id"))
            if original:
                for field in ("review_round", "ai_review_count"):
                    item[field] = max(int(item.get(field, 0)), int(original.get(field, 0)))
        return recovered

    def defer(self, parsed, filename):
        path = self.root / "txt_deferred" / (parsed["digest"] + ".json")
        save_session(path, {"task_id": parsed["meta"]["task_id"], "text": parsed["text"], "filename": _redact(str(filename))})

    def remove_deferred(self, digest):
        try:
            (self.root / "txt_deferred" / (digest + ".json")).unlink(missing_ok=True)
        except OSError:
            pass  # Replay remains idempotent.

    def deferred(self):
        rows = []
        for path in (self.root / "txt_deferred").glob("*.json"):
            data = load_session(path)
            if data and isinstance(data.get("text"), str):
                rows.append((data.get("filename", path.name), data["text"]))
        return rows


def accessible(path):
    try:
        os.stat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise OSError("资料暂不可访问，请检查硬盘或权限") from exc


def add_parts(task, ids, mode, value, supplement=False):
    sizes = split_sizes(len(ids), mode, value)
    added = []
    position = 0
    for size in sizes:
        number = len(task["parts"]) + 1
        part = {"part_id": uuid.uuid4().hex, "number": number,
                "ids": ids[position:position + size], "state": "export_pending",
                "supplement": supplement, "imports": [], "reason": ""}
        part["file"] = f"AI_REVIEW_{task['task_id'][:8]}_P{number:04d}_INPUT.txt"
        task["parts"].append(part)
        added.append(part)
        position += size
    for part in added:
        part["part_count"] = len(task["parts"])


def create_task(context, items, parent, mode="books", value=10, scope="all", supplements=None, continuation=""):
    if not items:
        raise ValueError("当前范围没有可复核的漫画")
    parent = Path(parent)
    if not parent.is_dir():
        raise ValueError("导出目录不可用，请重新选择")
    task_id = uuid.uuid4().hex
    task = {"task_id": task_id, "session_id": context.payload["session_id"], "library_id": context.payload.get('library_id', ''), "transport": "TXT",
            "created_at": datetime.now().isoformat(timespec="seconds"), "state": "export_failed",
            "scope": scope, "config": {"model": "外部AI（TXT）"}, "items": {}, "parts": [],
            "events": [], "split_mode": mode, "split_value": value,
            "supplements": copy.deepcopy(supplements or []), "continuation":continuation}
    directory = parent / ("AI_REVIEW_" + datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + task_id)
    backup = context.backup()
    try:
        directory.mkdir()  # Unique, never replace another task's files.
        task["export_dir"] = str(directory)
        for item in items:
            item.review_round += 1
            item.ai_special_flow = item.ai_special_flow or item.category in {CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED} or item.program_category in {CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED}
            task["items"][item.local_id] = {"input": input_item(item, task_id, item.review_round, task["supplements"]),
                                          "original_path": item.original_path, "state": "export_pending", "reason": ""}
            for prior in context.tasks:
                row = prior.get("items", {}).get(item.local_id)
                if row and row.get("state") in {"pending", "dispatching", "uncertain", "export_pending"}:
                    row.update(state="superseded", reason="已由较新审核轮次接替")
        add_parts(task, list(task["items"]), mode, value)
        for prior in context.tasks:
            if is_txt(prior):
                prior["state"] = task_state(prior)
        context.tasks.append(task)
        context.save()  # Identity and original inputs exist before writing INPUT.
    except Exception:
        context.rollback(backup)
        try:
            directory.rmdir()
        except OSError:
            pass
        raise
    return task


def export_parts(context, task, parent=None):
    if parent is not None:
        parent = Path(parent)
        if not parent.is_dir():
            raise ValueError("导出目录不可用，请重新选择")
        directory = parent / ("AI_REVIEW_" + task["task_id"] + "_补审_" + uuid.uuid4().hex[:8])
        directory.mkdir()
    else:
        directory = Path(task.get("last_export_dir", task["export_dir"]))
        if not directory.is_dir():
            raise ValueError("原导出目录不可用，请重新选择父目录")
    # An unwritten supplement must not resend comics resolved by a late RESULT.
    # Keep its identity in history and make a fresh part for unresolved comics.
    plan_backup = context.backup()
    planned = parent is not None
    try:
        for part in list(task["parts"]):
            if part["state"] != "export_pending" or not part["supplement"]:
                continue
            old_path = Path(part.get("planned_path", str(directory / part["file"])))
            try:
                confirmed_bytes = old_path.is_file() and old_path.read_bytes() == input_text(task, part).encode()
            except OSError:
                confirmed_bytes = False
            if confirmed_bytes:
                continue  # A previous write needs confirmation, not a resend.
            remaining = [i for i in part["ids"] if task["items"][i]["state"] in {"export_pending", "pending", "failed"}]
            if remaining != part["ids"]:
                part.update(state="cancelled", reason="部分漫画已取得有效结果或被接替，本份未重发；剩余项目另用新分片")
                if remaining:
                    add_parts(task, remaining, "books", len(remaining), supplement=True)
                planned = True
        if parent is not None:
            task["last_export_dir"] = str(directory)
        if planned:
            for part in task["parts"]:
                if part["state"] == "export_pending":
                    part["planned_path"] = str(directory / part["file"])
            task["state"] = task_state(task)
            context.save()
    except Exception:
        context.rollback(plan_backup)
        if parent is not None:
            directory.rmdir()
        raise
    backup = context.backup()
    try:
        for part in task["parts"]:
            if part["state"] != "export_pending":
                continue
            path = Path(part.get("planned_path", str(directory / part["file"])))
            try:
                text = input_text(task, part)
                if not path.is_file() or path.read_bytes() != text.encode():
                    atomic_text(path, text)
                part.update(state="exported", reason="", path=str(path),
                            exported_at=datetime.now().isoformat(timespec="seconds"),
                            sha256=hashlib.sha256(text.encode()).hexdigest())
                for local_id in part["ids"]:
                    row = task["items"][local_id]
                    if row["state"] == "export_pending":
                        row["state"] = "pending"
            except OSError as exc:
                part["reason"] = _redact(str(exc))
        task["last_export_dir"] = str(directory)
        task["state"] = task_state(task)
        context.save()
    except Exception:
        context.rollback(backup)
        raise OSError("文件可能已写出，但导出记录尚未确认保存；请重试失败分片，成功文件保留") from None


def supplement(context, task, selected, mode, value):
    eligible, expired = [], []
    if not accessible(context.payload.get("work_directory", "")):
        raise OSError("原工作目录暂不可访问，待恢复后补审")
    for local_id in dict.fromkeys(selected):
        row = task["items"].get(local_id)
        item = context.by_id.get(local_id)
        if not row or not item or row["state"] not in {"pending", "failed"}:
            continue
        ctrl = row["input"]["CONTROL"]
        if item.review_round != ctrl["review_round"]:
            continue
        if not accessible(item.original_path):
            continue
        if current_input_fingerprint(item, row["input"]) != ctrl["input_fingerprint"]:
            expired.append(local_id)
        else:
            eligible.append(local_id)
    if not eligible:
        return [], expired
    backup = context.backup()
    try:
        add_parts(task, eligible, mode, value, supplement=True)
        for local_id in eligible:
            row = task["items"][local_id]
            if row["state"] == "failed":
                row["previous_failure"] = row.get("reason", "")
            row.update(state="export_pending", reason="")
        task["state"] = task_state(task)
        context.save()
    except Exception:
        context.rollback(backup)
        raise
    return eligible, expired


def _whole_result(parsed, task, part):
    if parsed['errors'] or len(parsed['rows']) != len(part['ids']): return False
    lines = [line.strip() for line in parsed['text'].splitlines() if line.strip()]
    if not lines or lines[-1] != 'AI_REVIEW_RESULT_END': return False
    if lines.count('AI_REVIEW_RESULT_BEGIN') != 1 or lines.count('AI_REVIEW_RESULT_END') != 1: return False
    outputs = validate_results({'results':parsed['rows']},[task['items'][i]['input'] for i in part['ids']])
    return all(outputs[i].get('valid') for i in part['ids'])


def _previously_successful(parsed, task, part):
    if parsed['digest'] in part.get('successful_imports',[]): return True
    return any(event.get('event')=='TXT导入' and event.get('digest')==parsed['digest']
        and not event.get('block_errors') and all(event.get('items',{}).get(i,{}).get('state')=='success' for i in part['ids'])
        for event in task.get('events',[]))


def import_results(repository, files, context_provider):
    """One import batch; one logical commit per original session, one UI refresh."""
    summary = {key: 0 for key in ("files", "success", "failed", "stale", "superseded", "missing", "duplicate", "duplicate_files", "deferred", "file_errors", "applied")}
    summary["anomalies"] = []
    summary['file_outcomes'] = []
    groups = defaultdict(list)
    contexts = {}
    for filename, text in files:
        parsed, tid = None, None
        summary["files"] += 1
        try:
            parsed = parse_result(text)
            tid = parsed["meta"].get("task_id")
            if not isinstance(tid, str):
                raise ValueError("文件任务编号无效")
            context = context_provider(tid)
            sid = context.payload["session_id"]
            contexts.setdefault(sid, context)
            groups[sid].append((filename, parsed))
        except (ValueError, OSError, TypeError, KeyError, AttributeError) as exc:
            known = repository._tasks.get(tid)
            if known is not None and parsed is not None:
                task = known[2]
                part = next((p for p in task.get("parts", []) if p.get("part_id") == parsed["meta"].get("part_id")), None)
                try:
                    if part is None:
                        raise ValueError("无对应分片")
                    validate_meta(parsed["meta"], task, part)
                    repository.defer(parsed, filename)
                    summary["deferred"] += len(part["ids"])
                    summary["anomalies"].append({"file": _redact(str(filename)), "status": "待核对", "reason": _redact(str(exc)) + "；已保留结果，恢复原资料后重试"})
                    continue
                except (ValueError, OSError):
                    pass
            summary["file_errors"] += 1
            summary["anomalies"].append({"file": _redact(str(filename)), "status": "文件跳过", "reason": _redact(str(exc))})
    for sid, entries in groups.items():
        context = contexts[sid]
        backup = context.backup()
        delta = {key: 0 for key in summary if key not in {"files", "anomalies", "file_outcomes"}}
        file_outcomes = []
        anomalies, queued, finished = [], [], []
        outcomes, duplicates, deferred_ids, applied_ids = {}, set(), set(), set()
        try:
            for filename, parsed in entries:
                tid = parsed["meta"]["task_id"]
                task = next((t for t in context.tasks if t.get("task_id") == tid and is_txt(t)), None)
                part = next((p for p in (task or {}).get("parts", []) if p["part_id"] == parsed["meta"].get("part_id")), None)
                try:
                    if task is None or part is None or task.get("session_id") != sid:
                        raise ValueError("找不到文件对应的原分片")
                    validate_meta(parsed["meta"], task, part)
                except ValueError as exc:
                    delta["file_errors"] += 1
                    anomalies.append({"file": str(filename), "status": "文件跳过", "reason": str(exc)})
                    continue
                if parsed["digest"] in part["imports"]:
                    delta["duplicate_files"] += 1
                    duplicates.update((tid, i) for i in part["ids"])
                    finished.append(parsed["digest"])
                    eligible = (_whole_result(parsed,task,part) and _previously_successful(parsed,task,part)
                        and all(task['items'][i]['state']=='success' and i in context.by_id and context.by_id[i].review_round==task['items'][i]['input']['CONTROL']['review_round'] for i in part['ids']))
                    file_outcomes.append({'file':str(filename),'task_id':tid,'eligible':eligible,
                        'reason':'' if eligible else '重复文件未确认整份成功，或原轮次已被接替，原文件保留'})
                    continue
                if part["state"] != "exported":
                    path = Path(part.get("planned_path", str(Path(task["export_dir"]) / part["file"])))
                    expected = input_text(task, part).encode()
                    if not path.is_file() or path.read_bytes() != expected:
                        delta["file_errors"] += 1
                        anomalies.append({"file": str(filename), "status": "文件跳过", "reason": "原分片尚未确认成功导出，请先重试导出"})
                        continue
                    part.update(state="exported", path=str(path), sha256=hashlib.sha256(expected).hexdigest(),
                                export_confirmed_at=datetime.now().isoformat(timespec="seconds"))
                received = part.setdefault("received_results", [])
                if parsed["digest"] not in received:
                    received.append(parsed["digest"])
                try:
                    available = accessible(context.payload.get("work_directory", ""))
                except OSError:
                    available = False
                if not available:
                    deferred_ids.update((tid, i) for i in part["ids"])
                    queued.append((parsed, filename))
                    anomalies.append({"file": str(filename), "status": "待核对", "reason": "原工作目录暂不可访问，结果保留，其他任务继续"})
                    continue
                inputs = [task["items"][i]["input"] for i in part["ids"]]
                outputs = validate_results({"results": parsed["rows"]}, inputs)
                if parsed["errors"]:
                    anomalies.append({"file": str(filename), "status": "单本块格式异常", "reason": "；".join(parsed["errors"]) + "；其余完整项目继续处理"})
                deferred = False
                for local_id in part["ids"]:
                    row = task["items"][local_id]
                    item = context.by_id.get(local_id)
                    state, reason = "", ""
                    if row["state"] == "success":
                        duplicates.add((tid, local_id))
                        continue
                    if local_id in getattr(context, 'file_blocked_ids', set()):
                        deferred = True
                        deferred_ids.add((tid, local_id))
                        anomalies.append({'file': str(filename), 'title': row['input']['LOCAL']['local_name'],
                                          'status': '待核对', 'reason': '对应漫画正在文件处理，结果已保留；任务结束后可重新导入'})
                        continue
                    if row["state"] == "superseded" or (item and item.review_round != row["input"]["CONTROL"]["review_round"]):
                        state, reason = "superseded", "已有较新审核任务，旧结果已忽略"
                        if row["state"] != "success":
                            row.update(state="superseded", reason=reason)
                    elif not item or item.original_path != row.get("original_path"):
                        state, reason = "missing", "原漫画不存在或路径已改变，未替代匹配"
                        row.update(state="missing", reason=reason)
                    else:
                        try:
                            exists = accessible(item.original_path)
                        except OSError:
                            deferred = True
                            deferred_ids.add((tid, local_id))
                            anomalies.append({"file": str(filename), "title": item.original_name, "status": "待核对", "reason": "原漫画资料暂不可访问"})
                            continue
                        if not exists:
                            state, reason = "missing", "原漫画已不存在，结果已跳过"
                            row.update(state="missing", reason=reason)
                        else:
                            output = outputs[local_id]
                            if current_input_fingerprint(item, row["input"]) != row["input"]["CONTROL"]["input_fingerprint"]:
                                output = {"error": "送审后名称或来源已变化，需要重新复核", "error_code": "stale_result"}
                            apply_review_output(item, row, output, task)
                            state = "success" if output.get("valid") else ("stale" if output.get("error_code") == "stale_result" else "failed")
                            reason = row["reason"]
                            if row.get("apply") == "已应用":
                                applied_ids.add((tid, local_id))
                    outcomes[(tid, local_id)] = state
                    if state != "success" or row.get("apply", "").startswith("未应用"):
                        anomalies.append({"file": str(filename), "title": row["input"]["LOCAL"]["local_name"],
                                          "status": state, "reason": row.get("apply", "") if state == "success" else reason})
                if deferred:
                    queued.append((parsed, filename))
                else:
                    part["imports"].append(parsed["digest"])
                    finished.append(parsed["digest"])
                task["state"] = task_state(task)
                task["events"].append({"at": datetime.now().isoformat(timespec="seconds"), "event": "TXT导入", "part_id": part["part_id"], "digest": parsed["digest"], "block_errors": parsed["errors"],
                    "items": {i: {k: task["items"][i].get(k, "") for k in ("state", "decision", "reason", "apply", "error_code")} for i in part["ids"]}})
                eligible = not deferred and _whole_result(parsed,task,part) and all(task['items'][i]['state']=='success' and i in context.by_id and context.by_id[i].review_round==task['items'][i]['input']['CONTROL']['review_round'] for i in part['ids'])
                if eligible:
                    part.setdefault('successful_imports',[]).append(parsed['digest'])
                file_outcomes.append({'file':str(filename),'task_id':tid,'eligible':eligible,
                    'reason':'' if eligible else '文件存在格式、资料变化、旧轮次或待核对项目，原文件保留'})
            for state in outcomes.values():
                delta[state] += 1
            delta["duplicate"] = len(duplicates)
            delta["deferred"] = len(deferred_ids)
            delta["applied"] = len(applied_ids)
            context.save()
        except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
            context.rollback(backup)
            summary["file_errors"] += len(entries)
            summary["anomalies"].append({"status": "未确认保存", "reason": _redact(str(exc)) + "；该原会话本次修改已撤回，可重导RESULT"})
            continue
        for key, value in delta.items():
            summary[key] += value
        summary["anomalies"].extend(anomalies)
        summary['file_outcomes'].extend(file_outcomes)
        for parsed, filename in queued:
            try:
                repository.defer(parsed, filename)
            except OSError as exc:
                summary["anomalies"].append({"file": str(filename), "status": "待核对文件未保存", "reason": _redact(str(exc)) + "；请保留原RESULT文件"})
        for digest in finished:
            repository.remove_deferred(digest)
    return summary
