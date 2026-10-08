"""Read stable RESULT candidates off the GUI thread; never alter source files."""
from pathlib import Path
from .ai_txt import MAX_RESULT_BYTES, parse_result
from .run_log import _redact


def scan_results(directory, task_id, progress=None):
    result = {'task_id': task_id, 'files': [], 'other_tasks': 0, 'skipped': 0,
              'scanned': 0, 'anomalies': [], 'sources':{}}
    def skip(path, reason):
        result['skipped'] += 1
        result['anomalies'].append({'file': _redact(str(path)), 'status': '目录文件跳过', 'reason': reason})
    try:
        paths = sorted((p for p in Path(directory).iterdir() if p.suffix.lower() == '.txt' and p.is_file()), key=lambda p:p.name.casefold())
    except OSError as exc:
        skip(directory, _redact(str(exc)))
        return result
    for number, path in enumerate(paths, 1):
        result['scanned'] += 1
        try:
            if path.is_symlink(): raise ValueError('链接文件保留原位，不自动导入')
            before = path.stat()
            if before.st_size > MAX_RESULT_BYTES:
                raise ValueError('文件超过32MB，未读取')
            # Bounded read also handles a file growing after stat().
            with path.open('rb') as stream:
                raw = stream.read(MAX_RESULT_BYTES + 1)
            after = path.stat()
            if len(raw) > MAX_RESULT_BYTES:
                raise ValueError('文件超过32MB，未读取')
            if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino) or len(raw) != after.st_size:
                raise ValueError('文件仍在变化，请下载完成后再次导入')
            text = raw.decode('utf-8-sig')
            parsed = parse_result(text)
            if parsed['meta'].get('task_id') != task_id:
                result['other_tasks'] += 1
            elif not any(line.strip() == 'AI_REVIEW_RESULT_END' for line in text.splitlines()):
                skip(path, 'RESULT尚未完整结束，暂不自动导入；可在确认后手动选择文件')
            else:
                result['files'].append((str(path), text))
                import hashlib
                result['sources'][str(path)] = {'sha256':hashlib.sha256(raw).hexdigest(),
                    'signature':[after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns]}
        except (OSError, ValueError, UnicodeError) as exc:
            skip(path, _redact(str(exc)))
        if progress:
            progress(number, len(paths))
    return result
