"""Operation labels and local-volume safety; independent of the Qt UI."""
from pathlib import Path
import ctypes
import ntpath
import os
import stat
from .path_rules import path_key, name_problem

ACTIONS = {'rename': '改名', 'move': '移动', 'undo': '恢复原名', 'undo_move': '移回原处'}


def file_labels(value):
    value = getattr(value, 'file_status', value)
    return [x for x in ('已改名', '已移动') if x in str(value).split(' · ')]


def result_status(labels, action):
    labels = set(labels)
    if action == 'rename': labels.add('已改名')
    elif action == 'move': labels.add('已移动')
    if labels:
        return ' · '.join(x for x in ('已改名', '已移动') if x in labels)
    return '已撤销移动' if action == 'undo_move' else '已撤销'


def action_label(task):
    return ACTIONS.get(task.get('action'), '未知文件操作')


def within(path, parent):
    child, ancestor = path_key(os.path.abspath(path)), path_key(os.path.abspath(parent))
    return child == ancestor or child.startswith(ancestor.rstrip('\\') + '\\')


def local_path_problem(path):
    value = str(path)
    if value.startswith(('\\\\', '//')):
        return '本版不支持网络目录移动'
    if os.name == 'nt':
        drive = ntpath.splitdrive(os.path.abspath(path))[0] + '\\'
        if ctypes.windll.kernel32.GetDriveTypeW(drive) == 4:
            return '本版不支持映射网络盘移动'
    # Every ancestor is checked: a benign-looking child of a junction is unsafe too.
    current = Path(path)
    for part in (current, *current.parents):
        try:
            info = os.lstat(part)
            if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
                return '路径含符号链接 / 联接 / 重解析点，不能安全移动'
        except OSError as exc:
            return '路径不可访问：' + str(exc)
    return ''


def move_target_problem(source, target, identity, parent_identity=None, *, virtual=False):
    parent = os.path.dirname(target)
    if not os.path.isabs(source) or not os.path.isabs(target):
        return '移动路径必须是完整绝对路径'
    problem = name_problem(os.path.basename(target))
    if problem: return problem
    if len(str(target).encode('utf-16-le')) // 2 >= 260:
        return '完整目标路径过长（本版安全上限 259 个 UTF-16 单位）'
    if os.path.basename(source) != os.path.basename(target):
        return '移动必须保留当前名称，请先完成改名'
    if not os.path.isdir(parent):
        return '目标目录不存在或不是文件夹，请重新选择'
    if identity.get('kind') == 'directory' and within(parent, source):
        return '不能把漫画移动到自身或内部子目录'
    for path in (os.path.dirname(source), parent):
        problem = local_path_problem(path)
        if problem: return problem
    if not virtual:
        problem = local_path_problem(source)
        if problem: return problem
    try:
        if parent_identity:
            from .file_tasks import object_identity
            if object_identity(parent) != parent_identity:
                return '目标目录在预览后已变化，请重新检查'
    except OSError as exc:
        return '目标目录不可访问：' + str(exc)
    return ''
