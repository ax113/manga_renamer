"""Archive only transaction-confirmed RESULT sources with unchanged bytes."""
from __future__ import annotations
import hashlib
import os
import re
from pathlib import Path
from .ai_txt import MAX_RESULT_BYTES


def read_source(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file(): raise ValueError('不是普通RESULT源文件')
    before = path.stat()
    if before.st_size>MAX_RESULT_BYTES: raise ValueError('文件超过32MB，未读取')
    with path.open('rb') as stream: raw = stream.read(MAX_RESULT_BYTES+1)
    after = path.stat()
    signature = lambda s:(s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns)
    if len(raw)>MAX_RESULT_BYTES or signature(before)!=signature(after) or len(raw)!=after.st_size:
        raise ValueError('文件仍在变化，请下载完成后重试')
    return raw.decode('utf-8-sig'), {'sha256':hashlib.sha256(raw).hexdigest(),'signature':list(signature(after))}


def archive_source(filename, task_id, token, directory):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',task_id): raise ValueError('无法确认归档任务编号')
    source = Path(filename)
    _,current = read_source(source)
    if current!=token: raise ValueError('源文件已变化，保留原文件，请重新导入')
    folder = Path(directory)/('AI_REVIEW_'+task_id)
    folder.mkdir(parents=True,exist_ok=True)
    if source.parent.resolve()==folder.resolve(): return '已在归档目录',str(source)
    target = folder/source.name
    number = 1
    while True:
        try:
            fd = os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); break
        except FileExistsError:
            target = folder/(source.stem+f' ({number})'+source.suffix); number+=1
    try:
        with os.fdopen(fd,'wb') as output, source.open('rb') as incoming:
            size = 0
            while chunk:=incoming.read(64*1024):
                size += len(chunk)
                if size > token['signature'][2]:
                    raise ValueError('归档期间源文件发生变化，保留原文件')
                output.write(chunk)
            output.flush(); os.fsync(output.fileno())
        _,copied = read_source(target)
        _,unchanged = read_source(source)
        if copied['sha256']!=token['sha256'] or unchanged!=token:
            raise ValueError('归档期间源文件发生变化，保留原文件')
        source.unlink()
    except Exception:
        target.unlink(missing_ok=True)
        raise
    return '已归档',str(target)


def archive_results(report, files, sources, settings):
    summary = {'enabled':bool(settings.get('txt_archive_enabled',False)), 'archived':0,'retained':0,'failed':0}
    report['archive'] = summary
    if not summary['enabled']: return report
    outcomes = {row['file']:row for row in report.get('file_outcomes',[])}
    seen = set()
    for filename,text in files:
        if str(filename) in seen: continue
        seen.add(str(filename))
        row = outcomes.get(str(filename),{})
        if not row.get('eligible') or str(filename) not in sources:
            summary['retained']+=1
            if row.get('reason'):
                report['anomalies'].append({'file':str(filename),'status':'留待处理','reason':row['reason']})
            continue
        try:
            directory = settings.get('txt_archive_directory','')
            if not directory: raise ValueError('归档目录尚未设置，原文件保留')
            status,target = archive_source(str(filename),row['task_id'],sources[str(filename)],directory)
            summary['archived'] += status=='已归档'
            report['anomalies'].append({'file':str(filename),'status':status,'reason':target})
        except (OSError,ValueError) as exc:
            summary['failed']+=1
            report['anomalies'].append({'file':str(filename),'status':'归档失败','reason':str(exc)})
    return report


def archive_line(report):
    row = report.get('archive',{})
    return (f"已归档 {row.get('archived',0)} 份｜留待处理 {row.get('retained',0)} 份｜归档失败 {row.get('failed',0)} 份"
            if row.get('enabled') else '')
