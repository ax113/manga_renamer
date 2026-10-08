"""A persistent identity for a source database/work-directory combination."""
from pathlib import Path
import json
import os
import ntpath
import uuid
from .path_rules import windows_path
from .session_store import save_session


def canonical(path):
    value = str(path or '').strip()
    if not value:
        return ''
    if windows_path(value):
        return ntpath.normcase(ntpath.normpath(value))
    return os.path.normcase(os.path.abspath(value))


class LibraryRegistry:
    def __init__(self, root):
        self.path = Path(root) / 'libraries.json'

    def resolve(self, database, directory, preferred=''):
        if not str(directory or '').strip():
            return ''
        data = json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {'libraries': []}
        rows = data.get('libraries')
        if not isinstance(rows, list):
            raise ValueError('漫画库登记损坏，不能安全建立库身份')
        key = [canonical(database), canonical(directory)]
        old = next((r for r in rows if r.get('key') == key), None)
        if old:
            if preferred and preferred != old['library_id']:
                raise ValueError('会话漫画库身份与持久登记不一致')
            return old['library_id']
        if preferred and any(r.get('library_id') == preferred for r in rows):
            raise ValueError('该漫画库身份已绑定其他数据源')
        identity = preferred or ('L-' + uuid.uuid4().hex)
        rows.append({'library_id': identity, 'key': key,
                     'source_database': str(database), 'work_directory': str(directory)})
        save_session(self.path, data)
        return identity
