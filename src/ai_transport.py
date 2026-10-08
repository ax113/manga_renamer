"""Bounded serial Chat Completions transport; no Qt objects touched in workers."""

from __future__ import annotations

from .app_paths import data_dir, logs_dir

import copy
import queue
import json
import hashlib
import threading
import uuid
import http.client
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QObject, Signal

from .ai_review import RULE_VERSION, ReviewResponseError, parse_response_content, prompt_messages, validate_results
from .ai_log import TaskLog
from .ai_usage import response_usage
from .run_log import register_secret, _redact
from .ai_config import PROVIDERS, THINKING_MODES, modern_qwen, provider_for, automatic_provider, thinking_mode, request_options, runtime_values, validate_advanced
from .ai_errors import error_details
from .ai_schema import REVIEW_GUIDANCE


BATCH_SIZE = 10
TIMEOUT_SECONDS = 600
MAX_RETRIES = 2
MAX_SPLIT_DEPTH = 2
DEEPSEEK_MAX_TOKENS = 65536
GENERIC_MAX_TOKENS = 16000


def request_messages(config: dict, inputs: list[dict]) -> list[dict]:
    messages = prompt_messages(inputs)
    if automatic_provider(config) == "qwen":
        # Explicit output checks and restatements, without changing the shared
        # business input, frozen naming rules, or transport-independent guard.
        messages[0]["content"] += REVIEW_GUIDANCE
    return messages


def completion_url(endpoint: str) -> str:
    value = str(endpoint or "").strip().rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.fragment or parsed.query:
        raise ValueError("请输入有效的 HTTP(S) API 地址，且地址中不要包含账号或密钥")
    if value.endswith("/chat/completions"):
        return value
    return value + "/chat/completions"


def validate_config(config: dict[str, Any]) -> None:
    completion_url(config.get("endpoint", ""))
    validate_advanced(config)
    if not str(config.get("key") or "").strip() or not str(config.get("model") or "").strip():
        raise ValueError("请填写 Endpoint、API Key 和模型名称")
    if config.get("provider", "auto") not in {*PROVIDERS, "auto"}:
        raise ValueError("请选择有效的接口类型")
    if thinking_mode(config) not in THINKING_MODES:
        raise ValueError("请选择有效的思考模式")


def _retry_after(value: str) -> float:
    try:
        return min(45.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        try:
            when = parsedate_to_datetime(value)
            return min(45.0, max(0.0, (when - datetime.now(timezone.utc)).total_seconds()))
        except Exception:
            return 0.0


class ApiError(Exception):
    def __init__(self, message: str, *, transient: bool = False, global_error: bool = False,
                 retry_after: float = 0.0, http_status: int | None = None,
                 api_code: str = "", request_id: str = "", provider_message: str = "",
                 response: dict | None = None):
        super().__init__(message)
        self.transient = transient
        self.global_error = global_error
        self.retry_after = retry_after
        self.http_status = http_status
        self.api_code = api_code
        self.request_id = request_id
        self.provider_message = provider_message
        self.response = response


def runtime_config(config: dict, inputs: list[dict] | None = None) -> dict:
    register_secret(config.get("key", ""))
    options = request_options(config, inputs)
    limit_parameter = "max_completion_tokens" if "max_completion_tokens" in options else "max_tokens"
    result = {"endpoint": _redact(config["endpoint"]), "model": _redact(config["model"]),
              "profile_id": _redact(config.get("id", "")),
              "profile_name": _redact(config.get("name", "原有配置")),
              "provider": provider_for(config), "provider_selection": config.get("provider", "auto"),
              "thinking_mode": thinking_mode(config), **runtime_values(config),
              "effort": config.get("effort", "default"), "pricing": copy.deepcopy(config.get("pricing", {})),
              "custom_parameters": copy.deepcopy(config.get('custom_parameters',{})),
              "max_split_depth": MAX_SPLIT_DEPTH,
              "max_tokens": options.get(limit_parameter, 16000), "output_limit_parameter": limit_parameter,
              "thinking": options.get("thinking", "接口默认"),
              "enable_thinking": options.get("enable_thinking", "接口默认"),
              "reasoning_effort": options.get("reasoning_effort", "接口默认")}
    response_format = options.get('response_format')
    result['response_format'] = response_format.get('type','接口默认') if isinstance(response_format,dict) else '接口默认'
    if inputs is None and automatic_provider(config) == "qwen" and 'response_format' not in config.get('custom_parameters',{}):
        result["response_format"] = "json_schema"  # Nonempty formal review requests.
    if limit_parameter == "max_completion_tokens":
        result[limit_parameter] = options.get(limit_parameter, 16000)
    return result


class ResponseReadError(Exception):
    def __init__(self, partial: bytes, cause: Exception):
        self.partial, self.cause = partial, cause


def _read_response(response, deadline: float) -> bytes:
    # read1 returns available chunks instead of waiting for EOF. Reapply the
    # remaining budget to urllib's socket, so keep-alive whitespace cannot
    # restart the timeout indefinitely. Simple offline response fixtures use read.
    if not hasattr(response, "read1"):
        return response.read()
    chunks = []
    try:
        while True:
            remaining = deadline - perf_counter()
            if remaining <= 0:
                raise TimeoutError("API 返回等待达到上限")
            raw = getattr(getattr(response, "fp", None), "raw", None)
            socket = getattr(raw, "_sock", None)
            if socket is not None:
                socket.settimeout(remaining)
            chunk = response.read1(64 * 1024)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    except (OSError, http.client.IncompleteRead) as exc:
        partial = b"".join(chunks) + (exc.partial if isinstance(exc, http.client.IncompleteRead) else b"")
        raise ResponseReadError(partial, exc) from None


def post_completion(config: dict[str, str], inputs: list[dict], timeout: int = TIMEOUT_SECONDS,
                    *, audit: TaskLog | None = None, group: int = 0, attempt: int = 1) -> dict:
    validate_config(config)
    register_secret(config["key"])
    if audit is None:
        task_id = inputs[0]["CONTROL"]["task_id"] if inputs else "test_" + uuid.uuid4().hex
        audit = TaskLog(logs_dir(), task_id)
    url = completion_url(config["endpoint"])
    payload = {"model": config["model"].strip(), "messages": request_messages(config, inputs),
               "stream": False, **request_options(config, inputs)}
    request = urllib.request.Request(url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": "Bearer " + config["key"], "Content-Type": "application/json"}, method="POST")
    attempt_started = perf_counter()
    audit.request(group, attempt, payload, url, timeout, len(inputs))
    started = perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            # Read and archive the actual response before any JSON/size validation.
            try:
                data = _read_response(response, started + timeout)
            except ResponseReadError as exc:
                audit.response(group, attempt, exc.partial, received_complete=False,
                               http_ms=round((perf_counter() - started) * 1000, 2),
                               error_type=type(exc.cause).__name__)
                raise ApiError(f"API 返回未收完整（{type(exc.cause).__name__}）", transient=True) from None
            except http.client.IncompleteRead as exc:
                audit.response(group, attempt, exc.partial, received_complete=False,
                               http_ms=round((perf_counter() - started) * 1000, 2))
                raise ApiError("API 连接中断，返回内容未收完整", transient=True) from None
            status = getattr(response, "status", 200)
            headers = getattr(response, "headers", {})
            request_id = headers.get("X-Request-ID", "") or headers.get("X-Ds-Request-Id", "") or headers.get("X-DashScope-Request-Id", "")
        elapsed_ms = (perf_counter() - started) * 1000
        audit.response(group, attempt, data, http_status=status,
                       request_id=request_id, http_ms=round(elapsed_ms, 2),
                       received_complete=True, content_type=headers.get("Content-Type", "未提供"))
        if len(data) > 8 * 1024 * 1024:
            raise ApiError("API 返回超过本地解析上限；原文保存状态请查看任务日志")
        parsed = json.loads(data.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ApiError("API 外层返回不是 JSON 对象")
        if parsed.get("error") or (parsed.get("code") and not parsed.get("choices")):
            detail = error_details(parsed, status, request_id, provider=provider_for(config))
            audit.event("provider_error", group=group, attempt=attempt, **detail)
            raise ApiError(**detail, response=parsed)
        choices = parsed.get("choices")
        choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        audit.event("response_metadata", group=group, attempt=attempt,
                    response_id=parsed.get("id"), finish_reason=choice.get("finish_reason"),
                    usage=parsed.get("usage", "未提供"),
                    content_chars=len(message.get("content") or "") if isinstance(message.get("content"), str) else 0,
                    reasoning_chars=len(message.get("reasoning_content") or "") if isinstance(message.get("reasoning_content"), str) else 0)
        return parsed
    except urllib.error.HTTPError as exc:
        code = exc.code
        headers = exc.headers or {}
        retry = _retry_after(headers.get("Retry-After", ""))
        body, parsed = b"", {}
        request_id = headers.get("X-Request-ID", "") or headers.get("X-Ds-Request-Id", "") or headers.get("X-DashScope-Request-Id", "")
        try:
            body = _read_response(exc, started + timeout)
            audit.response(group, attempt, body, http_status=code, request_id=request_id,
                           received_complete=True, http_ms=round((perf_counter() - started) * 1000, 2))
        except Exception as read_error:
            if isinstance(read_error, http.client.IncompleteRead):
                audit.response(group, attempt, read_error.partial, http_status=code, received_complete=False)
            elif isinstance(read_error, ResponseReadError):
                audit.response(group, attempt, read_error.partial, http_status=code, received_complete=False)
            audit.exception("error_response_read_failed", read_error, group=group, attempt=attempt, http_status=code)
        try:
            value = json.loads(body.decode("utf-8"))
            parsed = value if isinstance(value, dict) else {}
        except (ValueError, UnicodeError):
            pass
        detail = error_details(parsed, code, request_id, provider=provider_for(config))
        audit.event("http_error", group=group, attempt=attempt, retry_after=retry, **detail)
        raise ApiError(**detail, retry_after=retry, response=parsed) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        audit.exception("connection_error", exc, group=group, attempt=attempt,
                        http_ms=round((perf_counter() - started) * 1000, 2))
        # Error string can contain userinfo or a echoed URL. Keep type only.
        raise ApiError(f"连接或等待 API 失败（{type(exc).__name__}）", transient=True) from None
    except (UnicodeError, ValueError) as exc:
        audit.exception("outer_response_parse_error", exc, group=group, attempt=attempt)
        raise ApiError(f"API 返回内容无法解析（{type(exc).__name__}）") from None
    finally:
        audit.event("attempt_end", group=group, attempt=attempt,
                    attempt_total_ms=round((perf_counter() - attempt_started) * 1000, 2))


class AiSignals(QObject):
    prepareGroup = Signal(str, int, object)
    groupReady = Signal(str, int, object, bool)
    done = Signal(str, str)
    diagnostic = Signal(str, object)


class ApiRunner(threading.Thread):
    """Concurrent HTTP groups; one coordinator owns durable result acknowledgements."""

    def __init__(self, task_id: str, items: list[dict], config: dict[str, str], signals: AiSignals,
                 *, log_root: Path | None = None, continuation: str = ""):
        super().__init__(name=f"api-review-{task_id[:8]}", daemon=True)
        self.task_id, self.items, self.signals = task_id, items, signals
        self.config = copy.deepcopy(config)
        self.runtime = runtime_values(config)
        self._validation_lock = threading.Lock()
        self.active_groups: set[int] = set()
        self.continuation = continuation
        self.stop_after_group = threading.Event()
        self.abandon = threading.Event()
        self._ack = threading.Event()
        self._ack_success = False
        self.superseded_ids: set[str] = set()
        self._attempt_counts: dict[str, int] = {}
        self._last_request_id = ""
        self._rules_hash = hashlib.sha256(request_messages(self.config, [])[0]["content"].encode()).hexdigest()
        self.log_root = log_root or logs_dir()
        self.audit = TaskLog(self.log_root, task_id,
                             lambda warning: self.signals.diagnostic.emit(task_id, warning))

    def acknowledge(self, saved: bool):
        self._ack_success = saved
        self._ack.set()

    def _wait_for_save(self, emit) -> bool:
        self._ack.clear()
        self._ack_success = False
        try:
            emit()
        except RuntimeError:
            self.abandon.set()
            return False
        self._ack.wait()
        return self._ack_success and not self.abandon.is_set()

    def _diagnostic(self, value: dict):
        try:
            self.signals.diagnostic.emit(self.task_id, value)
        except RuntimeError:
            self.abandon.set()  # Owner destroyed after explicit immediate exit.

    def _publish(self, number: int, results: dict, global_error: bool = False) -> bool:
        if not results:
            return True
        for local_id, output in results.items():
            output["attempts"] = self._attempt_counts.get(local_id, 0)
        return self._wait_for_save(lambda: self.signals.groupReady.emit(
            self.task_id, number, results, global_error))

    def _remaining_results(self, jobs: list[dict], reason: str) -> dict:
        return {entry["CONTROL"]["local_id"]:
                ({"error": reason, "error_code": "finish_length"}
                 if self._attempt_counts.get(entry["CONTROL"]["local_id"]) else {"not_sent": True})
                for job in jobs for entry in job["items"]
                if entry["CONTROL"]["local_id"] not in self.superseded_ids}

    def _request(self, number: int, group: list[dict], job: dict):
        audit = TaskLog(self.log_root, self.task_id, self.audit.on_warning)
        audit.context = {"root_group": job["root_group"], "parent_request": job.get("parent_request", ""), "split_depth": job["depth"]}
        for attempt in range(1, self.runtime["max_retries"] + 2):
            if self.abandon.is_set():
                return None, False, "interrupted"
            if self.stop_after_group.is_set() and (attempt > 1 or not job.get("dispatched")):
                results = {}
                for entry in group:
                    local_id = entry["CONTROL"]["local_id"]
                    results[local_id] = ({"error": "已停止后续请求，本组未取得有效结果", "error_code": "stopped_without_result"}
                                         if self._attempt_counts.get(local_id) else {"not_sent": True})
                return results, False, "stopped"
            self._diagnostic({"kind":"group_phase","group":number,"phase":"requesting","attempt":attempt})
            response, error, code, transient, global_error, retry_after = None, "", "", False, False, 0.0
            provider_error = {}
            validation_errors, valid_count = None, None
            started = perf_counter()
            started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
            for entry in group:
                local_id = entry["CONTROL"]["local_id"]
                self._attempt_counts[local_id] = self._attempt_counts.get(local_id, 0) + 1
            try:
                response = post_completion(self.config, group, timeout=self.runtime["timeout_seconds"], audit=audit, group=number, attempt=attempt)
                validation_started = perf_counter()
                with self._validation_lock:
                    results = validate_results(parse_response_content(response), group)
                valid_count = sum(bool(x.get("valid")) for x in results.values())
                validation_errors = {k: x.get("error") for k, x in results.items() if x.get("error")}
                audit.event("group_validation", group=number, attempt=attempt,
                                 validation_ms=round((perf_counter() - validation_started) * 1000, 2),
                                 valid=valid_count, errors=validation_errors)
            except ApiError as exc:
                error = str(exc)
                code = "api_" + exc.api_code if exc.api_code else "http_" + str(exc.http_status) if exc.http_status else "transport_error"
                response = exc.response
                provider_error = {"http_status": exc.http_status, "api_code": exc.api_code,
                                  "provider_request_id": exc.request_id, "provider_message": exc.provider_message}
                transient, global_error, retry_after = exc.transient, exc.global_error, exc.retry_after
                audit.event("attempt_failed", group=number, attempt=attempt, error=error,
                                 transient=transient, global_error=global_error, code=code, **provider_error)
            except ReviewResponseError as exc:
                error, code = str(exc), exc.code
                audit.exception("answer_invalid", exc, group=number, attempt=attempt, code=code)
            except Exception as exc:
                error, code = f"客户端处理异常（{type(exc).__name__}）", "client_processing_error"
                audit.exception("client_processing_error", exc, group=number, attempt=attempt)
            finally:
                choice = (response or {}).get("choices")
                choice = choice[0] if isinstance(choice, list) and choice and isinstance(choice[0], dict) else {}
                message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
                metric = {"request_id": f"group_{number:04d}_attempt_{attempt:02d}",
                          "group": number, "attempt": attempt, "root_group": job["root_group"],
                          "started_at": started_at, "finished_at": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                          "parent_request": job.get("parent_request", ""), "split_depth": job["depth"],
                          "item_count": len(group), "finish_reason": choice.get("finish_reason"),
                          "rule_version": RULE_VERSION, "rules_sha256": hashlib.sha256(request_messages(self.config,group)[0]["content"].encode()).hexdigest(),
                          "parameters": runtime_config(self.config, group),
                          "validated_items": valid_count,
                          "validation_failed_items": len(validation_errors) if validation_errors is not None else None,
                          "validation_errors": validation_errors,
                          "usage": response_usage(response or {}), "error": error, "error_code": code,
                          "content_chars": len(message.get("content") or "") if isinstance(message.get("content"), str) else 0,
                          "reasoning_chars": len(message.get("reasoning_content") or "") if isinstance(message.get("reasoning_content"), str) else 0,
                          "attempt_ms": round((perf_counter() - started) * 1000, 2), **provider_error}
                audit.event("request_metric", **metric)
                job["last_request_id"] = metric["request_id"]
                self._diagnostic({"kind": "request_metric", "metric": metric})
            if not error:
                return results, False, ""
            if transient and attempt <= self.runtime["max_retries"]:
                delay = retry_after or min(30, 2 ** (attempt - 1) * 3)
                waiting = perf_counter()
                audit.event("retry_wait_start", group=number, attempt=attempt, seconds=delay)
                self._diagnostic({"kind":"group_phase","group":number,"phase":"waiting","attempt":attempt,"seconds":delay})
                deadline = perf_counter() + delay
                while perf_counter() < deadline and not self.stop_after_group.is_set():
                    if self.abandon.wait(min(0.1, max(0, deadline - perf_counter()))):
                        return None, False, "interrupted"
                audit.event("retry_wait_end", group=number, attempt=attempt,
                                 wait_ms=round((perf_counter() - waiting) * 1000, 2))
                continue
            return {x["CONTROL"]["local_id"]: {"error": error, "error_code": code} for x in group}, global_error, code

    def run(self):
        started = perf_counter()
        batch_size = self.runtime["batch_size"]
        jobs = [{"items": self.items[start:start + batch_size], "root_group": start // batch_size + 1,
                 "parent_request": "", "depth": 0}
                for start in range(0, len(self.items), batch_size)]
        ready = queue.Queue()
        finish, number = "completed", 0
        accepting = True
        self.audit.event("task_start", total_items=len(self.items), continuation=self.continuation,
                         **runtime_config(self.config))

        def request_job(n, group, job):
            try:
                result = self._request(n, group, job)
            except Exception as exc:
                self.audit.exception("worker_failed", exc, group=n)
                result = ({entry["CONTROL"]["local_id"]: {"error":"请求线程异常，请查看日志", "error_code":"worker_error"}
                           for entry in group}, False, "worker_error")
            ready.put((n, group, job, result))

        while jobs or self.active_groups:
            if self.abandon.is_set():
                return  # HTTP workers are daemon threads; no claim of provider cancellation.
            if self.stop_after_group.is_set():
                accepting = False
                if finish == "completed":
                    finish = "stopped"
            while accepting and jobs and len(self.active_groups) < self.runtime["concurrency"]:
                if self.abandon.is_set() or self.stop_after_group.is_set():
                    accepting = False
                    break
                job = jobs.pop(0)
                group = [entry for entry in job["items"] if entry["CONTROL"]["local_id"] not in self.superseded_ids]
                if not group:
                    continue
                number += 1
                if not self._wait_for_save(lambda: self.signals.prepareGroup.emit(self.task_id, number, group)):
                    accepting, finish = False, "interrupted" if self.abandon.is_set() else "save_failed"
                    break
                # A stop can arrive while the GUI is saving prepareGroup. Don't send afterward.
                if self.stop_after_group.is_set():
                    self._publish(number, self._remaining_results([job], "已停止拆组补救"))
                    accepting, finish = False, "stopped"
                    break
                self.active_groups.add(number)
                job["dispatched"] = True
                self.audit.event("group_dispatched", group=number, in_flight=len(self.active_groups),
                                 queued_groups=len(jobs), queue_ms=round((perf_counter()-started)*1000,2))
                self._diagnostic({"kind":"in_flight", "groups":len(self.active_groups)})
                try:
                    threading.Thread(target=request_job, args=(number, group, job), daemon=True,
                                     name=f"api-{self.task_id[:8]}-{number}").start()
                except RuntimeError as exc:
                    self.active_groups.remove(number)
                    self.audit.exception("worker_start_failed", exc, group=number)
                    self._publish(number, {entry["CONTROL"]["local_id"]:{"not_sent":True} for entry in group})
                    accepting, finish = False, "paused"
                    self._diagnostic({"kind":"split_notice", "message":"系统无法启动更多请求线程，已暂停；请降低并发后接续。"})
            if not self.active_groups:
                break
            try:
                n, group, job, (results, global_error, code) = ready.get(timeout=0.1)
            except queue.Empty:
                continue
            self.active_groups.remove(n)
            self._diagnostic({"kind":"group_phase","group":n,"phase":"returned"})
            self._diagnostic({"kind":"in_flight", "groups":len(self.active_groups)})
            if results is None or self.abandon.is_set():
                return
            if code == "finish_length" and len(group) > 1 and job["depth"] < MAX_SPLIT_DEPTH and accepting and not self.stop_after_group.is_set():
                middle = len(group)//2
                parent = job["last_request_id"]
                children = [{"items":part,"root_group":job["root_group"],"parent_request":parent,"depth":job["depth"]+1}
                            for part in (group[:middle],group[middle:])]
                jobs[0:0] = children
                self.audit.event("group_split", group=n, parent_request=parent, child_sizes=[len(c["items"]) for c in children],
                                 child_local_ids=[[e["CONTROL"]["local_id"] for e in c["items"]] for c in children])
                self._diagnostic({"kind":"split_notice", "message":f"原第 {job['root_group']} 组输出达到上限，拆为 {middle}＋{len(group)-middle} 本补救。"})
                continue
            if not self._publish(n, results, global_error):
                accepting, finish = False, "interrupted" if self.abandon.is_set() else "save_failed"
            if global_error:
                accepting = False
                if finish != "save_failed":
                    finish = "paused"
        if self.abandon.is_set():
            return
        # Unsent original groups stay pending. Attempted split children are explicit failures.
        attempted = [{**job,"items":[e for e in job["items"] if self._attempt_counts.get(e["CONTROL"]["local_id"])]} for job in jobs]
        settled = self._remaining_results(attempted, "原组输出达到上限，后续拆组补救已停止")
        if settled and finish != "save_failed" and not self._publish(number, settled):
            finish = "save_failed"
        elapsed_ms = round((perf_counter()-started)*1000,2)
        self.audit.event("task_network_finished", state=finish, total_ms=elapsed_ms)
        self._diagnostic({"kind":"task_timing", "elapsed_ms":elapsed_ms})
        try:
            self.signals.done.emit(self.task_id, finish)
        except RuntimeError:
            self.abandon.set()
