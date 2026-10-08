"""Task-local evidence. No authentication headers, no automatic deletion."""
from __future__ import annotations

import json
import hashlib
import os
import platform
import re
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

from .run_log import append_rotating, log_message, redact_bytes, _redact
from .version import VERSION
from .ai_review import RULE_VERSION
from .ai_usage import aggregate_usage, usage_lines
from .ai_cost import estimate_cost, cost_line
from .ai_config import PROVIDERS, THINKING_MODES


def task_log_dir(log_root: Path, task_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
        raise ValueError("无效的日志任务编号")
    return Path(log_root) / "ai" / task_id


class TaskLog:
    def __init__(self, log_root: Path, task_id: str,
                 on_warning: Callable[[dict], None] | None = None):
        self.task_id = task_id
        self.directory = task_log_dir(log_root, task_id)
        self.on_warning = on_warning
        self.context: dict = {}

    def _warn(self, kind: str, exc: Exception):
        detail = _redact(str(exc))
        warning = {"kind": kind, "message": f"{kind}：{type(exc).__name__} {detail}"}
        log_message(f"AI 任务 {self.task_id} {warning['message']}", "ERROR")
        if self.on_warning:
            try:
                self.on_warning(warning)
            except RuntimeError:
                pass  # Window already destroyed after an explicit immediate exit.

    def event(self, event: str, **fields) -> bool:
        data = {"at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "task_id": self.task_id, "version": VERSION, "event": event, **self.context, **fields}
        try:
            append_rotating(self.directory, "events",
                            json.dumps(data, ensure_ascii=False), ".jsonl")
            return True
        except (OSError, ValueError, TypeError) as exc:
            self._warn("诊断日志未保存", exc)
            return False

    def file(self, name: str, body: bytes, kind: str) -> dict:
        temp = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            cleaned, redacted = redact_bytes(body)
            with tempfile.NamedTemporaryFile(dir=self.directory, prefix=".writing_",
                                             suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                stream.write(cleaned)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self.directory / name)
            result = {"saved": True, "file": name, "redacted": redacted,
                      "received_bytes": len(body), "stored_bytes": len(cleaned)}
            self.event("evidence_saved", kind=kind, **result)
            return result
        except (OSError, UnicodeError) as exc:
            self._warn(f"{kind}未保存", exc)
            return {"saved": False, "file": name, "error_type": type(exc).__name__}
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass

    def request(self, group: int, attempt: int, payload: dict, endpoint: str, timeout: int, item_count: int):
        stem = f"group_{group:04d}_attempt_{attempt:02d}"
        self.file(stem + "_request.json",
                  json.dumps(payload, ensure_ascii=False, indent=2).encode(), "请求输入")
        self.event("request_start", group=group, attempt=attempt, endpoint=endpoint,
                   model=payload.get("model"), item_count=item_count,
                   timeout_seconds=timeout, max_tokens=payload.get("max_tokens"),
                   max_completion_tokens=payload.get("max_completion_tokens"),
                   enable_thinking=payload.get("enable_thinking", "接口默认"),
                   thinking=payload.get("thinking", "接口默认"),
                   reasoning_effort=payload.get("reasoning_effort", "接口默认"),
                   response_format=payload.get("response_format", {}).get("type", "接口默认"),
                   response_schema_sha256=hashlib.sha256(json.dumps(
                       payload["response_format"]["json_schema"], ensure_ascii=False,
                       sort_keys=True).encode()).hexdigest() if "json_schema" in payload.get("response_format", {}) else "",
                   rule_version=RULE_VERSION,
                   rules_sha256=hashlib.sha256(payload["messages"][0]["content"].encode()).hexdigest(),
                   messages_sha256=hashlib.sha256(json.dumps(payload["messages"], ensure_ascii=False,
                                                           sort_keys=True).encode()).hexdigest(),
                   system=platform.platform(), python=platform.python_version())

    def response(self, group: int, attempt: int, body: bytes, **metadata):
        result = self.file(f"group_{group:04d}_attempt_{attempt:02d}_response.txt",
                           body, "原始返回")
        self.event("response_received", group=group, attempt=attempt, **metadata, **result)
        return result

    def exception(self, event: str, exc: Exception, **fields):
        self.event(event, error_type=type(exc).__name__, error=str(exc),
                   errno=getattr(exc, "errno", None), winerror=getattr(exc, "winerror", None),
                   traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
                   **fields)

    def summary(self, task: dict, *, final_state_saved: bool = True):
        """A replaceable index, never a substitute for the saved comic session."""
        rows = list(task.get("items", {}).values())
        counts = {state: sum(row.get("state") == state for row in rows)
                  for state in ("success", "failed", "pending", "dispatching", "uncertain", "superseded", "missing")}
        requests = list(task.get("requests", []))
        failures = {}
        for row in rows:
            if row.get("state") == "failed":
                reason = str(row.get("reason") or "未知原因")
                failures[reason] = failures.get(reason, 0) + 1
        report = dict(task_id=self.task_id, version=VERSION, rule_version=RULE_VERSION,
                      created_at=task.get("created_at"), state=task.get("state"),
                      elapsed_ms=task.get("elapsed_ms"),
                      final_state_saved=final_state_saved, continuation=task.get("continuation", ""),
                      unique_items=len(rows), counts=counts, failures=failures,
                      config=task.get("config", {}), usage=aggregate_usage(requests), requests=requests,
                      supplements=task.get("supplements", []), cost_estimate=estimate_cost(task))
        report["rules_sha256"] = sorted({row["rules_sha256"] for row in requests if row.get("rules_sha256")})
        report["known_attempt_total_ms"] = round(sum(row.get("attempt_ms", 0) for row in requests), 2)
        lines = [f"AI 任务 {self.task_id}", f"版本：{VERSION}｜规则：{RULE_VERSION}",
                 f"状态：{task.get('state', '')}｜本次关键状态确认保存：{'是' if final_state_saved else '否'}",
                 f"漫画：{len(rows)} 本｜成功 {counts['success']}｜失败 {counts['failed']}｜未发送 {counts['pending']}｜处理中 {counts['dispatching']}｜未确认 {counts['uncertain']}｜被接替 {counts['superseded']}｜已不存在 {counts['missing']}"]
        config = task.get("config", {})
        lines += [f"配置：{config.get('profile_name', '旧记录未提供')}｜接口：{PROVIDERS.get(config.get('provider'), '旧记录未提供')}",
                  f"模型：{config.get('model', '旧记录未提供')}｜推理：{THINKING_MODES.get(config.get('thinking_mode'), '旧记录未提供')}"]
        if task.get("continuation"):
            lines.append("接续旧任务：" + task["continuation"])
        lines += usage_lines(requests)
        lines.append(cost_line(task))
        if isinstance(task.get("elapsed_ms"), (int, float)):
            lines.append(f"任务运行：{task['elapsed_ms'] / 1000:.2f} 秒（含请求、重试等待及逐组保存）")
        lines += ["", "当前失败原因：", *[f"{count} 本：{reason}" for reason, count in failures.items()],
                  "", "实际请求（含重试/拆组）："]
        for request in requests:
            usage = request.get("usage", {})
            finish = request.get("finish_reason") or "未提供"
            conclusion = finish
            if request.get("error"):
                conclusion += "｜" + request["error"]
            if finish == "length" and request.get("content_chars") == 0:
                conclusion += "｜无正式答案"
                if usage.get("output") and usage.get("reasoning") == usage.get("output"):
                    conclusion += "｜输出全部用于思考"
            lines.append(f"{request['request_id']}｜原组 {request.get('root_group')}｜父请求 {request.get('parent_request') or '无'}｜{request.get('item_count')} 本｜{conclusion}")
        lines += ["", "摘要是诊断索引，漫画结果以已可靠保存的会话为准。缺少用量字段不计作零；累计包含失败和重试。"]
        self.file("summary.json", json.dumps(report, ensure_ascii=False, indent=2).encode(), "任务摘要")
        self.file("summary.txt", "\n".join(lines).encode(), "任务摘要")
