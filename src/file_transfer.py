"""Verified, resumable cross-volume transfers. Never merge or replace a target."""
from __future__ import annotations
import hashlib
import os
from pathlib import Path
import shutil
import stat
import uuid

CHUNK = 1024 * 1024


class TransferStopped(RuntimeError):
    pass


def stamp(path):
    st = os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or getattr(st, 'st_file_attributes', 0) & 0x400:
        raise ValueError('漫画内部含符号链接 / 联接 / 重解析点，未移动')
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        raise ValueError('漫画内部含不支持的特殊文件，未移动')
    return dict(device=int(st.st_dev), inode=int(st.st_ino), size=st.st_size,
                mtime=st.st_mtime_ns, kind='dir' if stat.S_ISDIR(st.st_mode) else 'file')


def inventory(path):
    result = {'': stamp(path)}
    if result['']['kind'] == 'dir':
        for parent, dirs, files in os.walk(path, followlinks=False):
            for name in sorted(dirs + files):
                p = os.path.join(parent, name)
                result[os.path.relpath(p, path)] = stamp(p)
    return result


def digest(path, stop, progress=None):
    before = stamp(path)
    value = hashlib.sha256()
    with open(path, 'rb') as stream:
        while True:
            if stop.is_set():
                raise TransferStopped('已停止；完整源文件和已复制临时文件保留')
            block = stream.read(CHUNK)
            if not block: break
            value.update(block)
            if progress: progress(len(block))
    if stamp(path) != before:
        raise ValueError('校验过程中内容发生变化，源文件未删除')
    return value.hexdigest()


def sync_directory(path):
    if os.name=='nt': return
    fd=os.open(path,os.O_RDONLY | getattr(os,'O_DIRECTORY',0))
    try: os.fsync(fd)
    finally: os.close(fd)


def child(root, relative):
    # Persisted manifests are not permission to operate outside their root.
    if relative and (os.path.isabs(relative) or '..' in Path(relative).parts):
        raise ValueError('临时清单路径不安全')
    return os.path.join(root, relative) if relative else root


def transfer_published(entry):
    return entry.get('transfer', {}).get('phase') in {'published', 'cleanup', 'cleanup_pending', 'done'}


def recover_transfer(entry, identity):
    from .file_tasks import same_identity
    t = entry['transfer']
    stage, target = t['staging_path'], entry['target_path']
    owned = t.get('staging_identity')
    if t['phase'] == 'publishing' and same_identity(target, owned) and not os.path.lexists(stage):
        t['phase'] = 'published'
    if transfer_published(entry):
        if not same_identity(target, owned):
            entry.update(state='uncertain', reason='完整目标身份已变化，请核对；不会删除源文件')
        elif not os.path.lexists(entry['source_path']):
            # Verify contents on next run before declaring cleanup complete.
            entry.update(state='unexecuted', reason='目标已发布；继续核验并确认源清理结果')
        else:
            entry.update(state='unexecuted', reason='目标已发布；继续核验并仅处理源清理')
        entry['result_identity'] = owned
    elif same_identity(entry['source_path'], identity) and (not os.path.lexists(stage) or same_identity(stage, owned)):
        entry.update(state='unexecuted', reason='源对象保留；继续核验已复制文件后续传')
    else:
        entry.update(state='uncertain', reason='源对象或临时对象身份已变化，请先核对')


def cross_volume_move(entry, identity, checkpoint, stop, report, rename):
    from .file_tasks import object_identity, same_identity
    from .file_operations import local_path_problem
    source, target = entry['source_path'], entry['target_path']
    parent = os.path.dirname(target)
    for p in (os.path.dirname(source), parent):
        problem = local_path_problem(p)
        if problem: raise ValueError(problem)
    if entry.get('target_parent_identity') and not same_identity(parent, entry['target_parent_identity']):
        raise ValueError('目标目录身份已变化，未继续移动')
    t = entry.get('transfer')
    if not t:
        if not same_identity(source, identity): raise ValueError('源对象身份已变化')
        manifest = inventory(source)
        token = uuid.uuid4().hex
        suffix = Path(source).suffix if identity['kind'] == 'archive' else ''
        staging = os.path.join(parent, '.manga-transfer-' + token + suffix)
        t = entry['transfer'] = dict(phase='creating', staging_path=staging, manifest=manifest,
                                    copied={}, total=sum(s['size'] for s in manifest.values() if s['kind']=='file'))
        checkpoint()
    stage = t['staging_path']
    if os.path.dirname(stage) != parent or not Path(stage).name.startswith('.manga-transfer-'):
        raise ValueError('临时路径记录不安全')
    manifest = t['manifest']
    for rel in manifest: child(source, rel); child(stage, rel)
    total = t['total']
    if t['phase'] == 'publishing' and same_identity(target, t.get('staging_identity')) and not os.path.lexists(stage):
        t['phase'] = 'published'; checkpoint()
    published = transfer_published(entry)
    destination = target if published else stage
    if published:
        if not same_identity(target, t.get('staging_identity')):
            raise ValueError('完整目标身份已变化，源清理未继续')
    else:
        if not same_identity(source, identity): raise ValueError('源对象身份已变化')
        if os.path.lexists(target): raise FileExistsError('目标已出现同名对象，未覆盖；源和临时文件保留')
        if t['phase'] == 'creating':
            if os.path.lexists(stage): raise ValueError('临时对象未确认归属，未写入')
            if identity['kind']=='directory': os.mkdir(stage)
            else:
                with open(stage, 'xb'): pass
            t['staging_identity'] = object_identity(stage)
            t['phase'] = 'copying'; checkpoint()
        if not same_identity(stage, t.get('staging_identity')):
            raise ValueError('临时对象身份已变化，未写入')
        if inventory(source) != manifest:
            raise ValueError('源目录清单 / 内容信息已变化，未继续复制')
        missing = sum(s['size'] for r,s in manifest.items() if s['kind']=='file' and r not in t['copied'])
        if shutil.disk_usage(parent).free < missing:
            raise OSError('目标磁盘剩余空间不足；源和临时文件保留')
        copied = sum(manifest[r]['size'] for r in t['copied'])
        report('copying', copied, total)
        for rel, info in manifest.items():
            if stop.is_set(): raise TransferStopped('已停止复制；源和临时文件保留')
            dst, src = child(stage, rel), child(source, rel)
            if info['kind']=='dir':
                if rel:
                    if not os.path.lexists(dst): os.mkdir(dst)
                    elif stamp(dst)['kind'] != 'dir': raise ValueError('临时目录类型已变化')
                continue
            if rel in t['copied']:
                if digest(src, stop) != t['copied'][rel] or digest(dst, stop) != t['copied'][rel]:
                    raise ValueError('已复制文件校验失败；未重复覆盖或删除源文件')
                continue
            # Only an incomplete file in our owned staging tree may be restarted.
            if os.path.lexists(dst) and stamp(dst)['kind']!='file': raise ValueError('临时文件类型已变化')
            problem=local_path_problem(os.path.dirname(dst))
            if problem: raise ValueError(problem)
            before = stamp(src)
            if before != info: raise ValueError('复制前源文件信息已变化')
            value = hashlib.sha256()
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0)
            with open(src, 'rb') as inp, os.fdopen(os.open(dst, flags, 0o600), 'wb') as out:
                current = 0
                while True:
                    if stop.is_set(): raise TransferStopped('已停止复制；源和临时文件保留')
                    block = inp.read(CHUNK)
                    if not block: break
                    out.write(block); value.update(block); current += len(block)
                    report('copying', copied + current, total)
                out.flush(); os.fsync(out.fileno())
            if stamp(src) != before: raise ValueError('复制过程中源文件变化，未删除')
            shutil.copystat(src, dst, follow_symlinks=False)
            t['copied'][rel] = value.hexdigest(); copied += info['size']; checkpoint()
        # Directory timestamps are applied after children have been created.
        for rel, info in sorted(manifest.items(), key=lambda x: len(Path(x[0]).parts), reverse=True):
            if info['kind']=='dir': shutil.copystat(child(source, rel), child(stage, rel), follow_symlinks=False)
        t['phase']='verifying'; checkpoint()
    # Always verify a previously published target before touching remaining source.
    actual = inventory(destination)
    if set(actual) != set(manifest) or any(actual[r]['kind']!=s['kind'] or (s['kind']=='file' and actual[r]['size']!=s['size']) for r,s in manifest.items()):
        raise ValueError('目标清单 / 文件数量 / 大小校验失败，源文件未删除')
    verified = 0
    report('verifying', 0, total)
    for rel, info in manifest.items():
        if info['kind']!='file': continue
        # Report byte progress per file; digest reads in bounded chunks.
        current = [0]
        def chunk(n): current[0] += n; report('verifying', verified + current[0], total)
        if digest(child(destination, rel), stop, chunk) != t['copied'].get(rel):
            raise ValueError('目标 SHA-256 内容校验失败，源文件未删除')
        if not published and digest(child(source, rel), stop) != t['copied'][rel]:
            raise ValueError('源内容发生变化，源文件未删除')
        verified += info['size']
    if not published:
        if inventory(source) != manifest: raise ValueError('源清单发生变化，未发布目标')
        for rel,info in sorted(manifest.items(),key=lambda x:len(Path(x[0]).parts),reverse=True):
            if info['kind']=='dir': sync_directory(child(stage,rel))
        t['phase']='publishing'; checkpoint()
        rename(stage, target)
        if not same_identity(target, t['staging_identity']): raise RuntimeError('发布后的目标身份无法核验')
        sync_directory(parent)
        t['phase']='published'; entry['result_identity']=t['staging_identity']; checkpoint()
    else: entry['result_identity']=t['staging_identity']
    t['phase']='cleanup'; checkpoint()
    try:
        if os.path.lexists(source):
            if not same_identity(source, identity): raise ValueError('剩余源对象身份已变化，未删除')
            remaining = inventory(source)
            if not set(remaining) <= set(manifest): raise ValueError('源目录出现新增文件，保留源目录等待处理')
            for rel, info in sorted(manifest.items(), key=lambda x: len(Path(x[0]).parts), reverse=True):
                if stop.is_set(): raise TransferStopped('目标已完整验证；源清理暂停，继续时只核验和清理')
                src = child(source, rel)
                if not os.path.lexists(src): continue
                found = stamp(src)
                if info['kind']=='dir':
                    if any(found[k]!=info[k] for k in ('device','inode','kind')): raise ValueError('源目录身份变化，未删除')
                    os.rmdir(src)
                else:
                    if found != info or digest(src, stop) != t['copied'][rel] or stamp(src)!=info:
                        raise ValueError('源文件变化，未删除')
                    if digest(child(target,rel),stop)!=t['copied'][rel]: raise ValueError('源清理前目标内容已变化，未删除剩余源文件')
                    os.unlink(src)
                report('cleanup', total, total)
        sync_directory(os.path.dirname(source))
        t['phase']='done'; checkpoint()
        report('done', total, total)
        return entry['result_identity']
    except (OSError, ValueError, TransferStopped):
        t['phase']='cleanup_pending'; checkpoint()
        raise
