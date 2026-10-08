"""Shared Windows name/path rules used by previews and real operations."""
import ntpath
import os
import re


def windows_path(path):
    return bool(re.match(r'^[A-Za-z]:[\\/]', str(path or '')) or '\\' in str(path or ''))


def path_key(path):
    value = ntpath.normpath(str(path or '').replace('/', '\\'))
    drive, tail = ntpath.splitdrive(value)
    return drive.casefold() + '\\' + '\\'.join(
        p.rstrip(' .').casefold() for p in tail.split('\\') if p not in {'', '.'})


def name_problem(name):
    raw = str(name or '')
    if not raw or raw in {'.', '..'}:
        return '名称为空或不是有效名称'
    if raw != raw.rstrip(' .'):
        return '名称末尾含 Windows 不允许的空格或句点'
    if any(c in raw for c in '<>:"/\\|?*') or any(ord(c) < 32 for c in raw):
        return '名称含 Windows 非法字符'
    base = raw.split('.', 1)[0].casefold()
    reserved = {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)),
                *(f'lpt{i}' for i in range(1, 10)), 'com¹', 'com²', 'com³', 'lpt¹', 'lpt²', 'lpt³'}
    if base in reserved:
        return '名称属于 Windows 保留设备名'
    if len(raw.encode('utf-16-le')) // 2 > 255:
        return '文件名过长（超过 255 个 UTF-16 单位）'
    return ''


def target_problem(source, target):
    module = ntpath if windows_path(source) else os.path
    if path_key(module.dirname(source)) != path_key(module.dirname(target)):
        return '本版只支持同一父目录内原地改名'
    problem = name_problem(module.basename(target))
    if problem:
        return problem
    # Do not silently add long-path prefixes or shorten a user's chosen name.
    if len(str(target).encode('utf-16-le')) // 2 >= 260:
        return '完整目标路径过长（本版安全上限 259 个 UTF-16 单位）'
    return ''
