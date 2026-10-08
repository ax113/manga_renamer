"""Streaming, paged historical log search and safe diagnostics for one task."""
import copy
import json
import threading
from pathlib import Path
from datetime import datetime
from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QComboBox,QLineEdit,
    QLabel,QPushButton,QPlainTextEdit,QApplication)
from .ui_common import AlignedComboBox
from .run_log import _redact
from .ai_log import task_log_dir
from .ai_cost import estimate_cost


def clean(value):
    if isinstance(value,dict):
        return {k:('[REDACTED]' if k.lower() in {'key','api_key','authorization'} else clean(v)) for k,v in value.items()}
    if isinstance(value,list): return [clean(v) for v in value]
    return value


def diagnostic_text(task):
    if not task: return '未选择任务'
    payload = copy.deepcopy(task)
    payload['cost_estimate'] = estimate_cost(task) if task.get('transport')!='TXT' else {'label':'TXT不估算订阅费用'}
    return _redact(json.dumps(clean(payload),ensure_ascii=False,indent=2))


def log_directories(owner,task_id):
    roots = owner.review_log_roots() if hasattr(owner,'review_log_roots') else [Path(owner.tool_dir())/'logs']
    return [task_log_dir(Path(root),task_id) for root in roots]


def search_logs(directories,query='',event='',after='',before='',page=0,page_size=300,cancelled=lambda:False):
    """Stream all volumes; retain just the requested page, so huge logs stay bounded."""
    query = query.casefold(); matches = 0; result = []; errors = []
    start = page*page_size
    try:
        lower = datetime.fromisoformat(after).astimezone() if after else None
        upper = datetime.fromisoformat(before).astimezone() if before else None
    except ValueError:
        return [],0,['时间格式应为 YYYY-MM-DD 或 YYYY-MM-DDTHH:MM:SS']
    if lower and upper and lower>upper: return [],0,['开始时间不能晚于结束时间']
    for directory in directories:
        for path in sorted(Path(directory).glob('events*.jsonl')):
            try:
                with path.open(encoding='utf-8',errors='replace') as stream:
                    for number,line in enumerate(stream,1):
                        if cancelled(): return [],0,[]
                        try:
                            row = json.loads(line)
                            tag = str(row.get('event',''))
                            at = datetime.fromisoformat(row.get('at','')).astimezone()
                        except (ValueError,TypeError,AttributeError):
                            row,tag,at = {'unparsed':line.rstrip()},'无法解析',None
                        if event=='errors':
                            if not (row.get('error') or 'failed' in tag or 'invalid' in tag or 'error' in tag or 'warning' in tag): continue
                        elif event and event.casefold() not in tag.casefold(): continue
                        if lower and (not at or at<lower): continue
                        if upper and (not at or at>upper): continue
                        text = _redact(json.dumps(clean(row),ensure_ascii=False))
                        if query and query not in text.casefold(): continue
                        if start<=matches<start+page_size: result.append(f'{path.name}:{number}  {text}')
                        matches += 1
            except OSError as exc:
                errors.append(f'{path.name}：{type(exc).__name__}')
    return result,matches,errors


class LogViewer(QDialog):
    loaded = Signal(int,object)
    def __init__(self,owner,task_id,parent=None):
        super().__init__(parent)
        self.owner = owner; self._token = 0; self.page = 0
        self.setWindowTitle('任务日志查找'); self.resize(960,630)
        root = QVBoxLayout(self)
        self.tasks = AlignedComboBox()
        for task in sorted(owner.review_tasks(),key=lambda t:t.get('created_at',''),reverse=True):
            self.tasks.addItem(f"{task.get('created_at','')[:16]} {'TXT' if task.get('transport')=='TXT' else 'API'} {task['task_id'][:8]}",task['task_id'])
        known = {self.tasks.itemData(i) for i in range(self.tasks.count())}
        roots = owner.review_log_roots()
        orphaned = set()
        for root_path in roots:
            directory = Path(root_path)/'ai'
            if directory.is_dir():
                for folder in directory.iterdir():
                    if folder.is_dir() and folder.name not in known: orphaned.add(folder.name)
        for tid in sorted(orphaned):
            try:
                task_log_dir(Path(roots[0]),tid)
            except ValueError:
                continue
            self.tasks.addItem('旧日志（会话不在当前索引）｜'+tid,tid)
        self.tasks.setCurrentIndex(max(0,self.tasks.findData(task_id))); root.addWidget(self.tasks)
        filters = QHBoxLayout()
        self.query = QLineEdit(); self.query.setPlaceholderText('关键词、请求编号或漫画编号')
        self.event = AlignedComboBox(); self.event.setEditable(True)
        for label,value in [('所有事件',''),('失败 / 异常','errors'),('请求','request'),('重试等待','retry_wait'),('核验','validation'),('结果应用','group_applied'),('可靠保存','commit')]: self.event.addItem(label,value)
        task = owner.review_task(task_id)
        if task and any(r.get('state')=='failed' for r in task.get('items',{}).values()): self.event.setCurrentIndex(1)
        self.after = QLineEdit(); self.after.setPlaceholderText('开始 YYYY-MM-DDTHH:MM')
        self.before = QLineEdit(); self.before.setPlaceholderText('结束 YYYY-MM-DDTHH:MM')
        for w in (self.query,self.event,self.after,self.before): filters.addWidget(w,1)
        root.addLayout(filters)
        buttons = QHBoxLayout()
        search = QPushButton('查找'); search.clicked.connect(self.reset_search)
        self.previous = QPushButton('上一页'); self.previous.clicked.connect(lambda:self.move(-1))
        self.next = QPushButton('下一页'); self.next.clicked.connect(lambda:self.move(1))
        copy_button = QPushButton('复制当前页（脱敏）'); copy_button.clicked.connect(lambda:QApplication.clipboard().setText(_redact(self.output.toPlainText())))
        for w in (search,self.previous,self.next,copy_button): buttons.addWidget(w)
        root.addLayout(buttons)
        self.status = QLabel(''); self.status.setWordWrap(True); root.addWidget(self.status)
        self.output = QPlainTextEdit(); self.output.setReadOnly(True); root.addWidget(self.output,1)
        self.tasks.currentIndexChanged.connect(self.reset_search)
        self.query.returnPressed.connect(self.reset_search)
        self.loaded.connect(self.show_result)
        self.reset_search()

    def reset_search(self,*_):
        self.page = 0; self.search()

    def move(self,direction):
        self.page = max(0,self.page+direction); self.search()

    def search(self):
        self._token += 1; token = self._token
        selected = self.tasks.currentData()
        if not selected:
            self.status.setText('尚无可查询的任务日志'); self.output.clear()
            self.previous.setEnabled(False); self.next.setEnabled(False)
            return
        dirs = log_directories(self.owner,selected)
        # Snapshot all Qt fields before launching the file-only thread.
        event = self.event.currentData() if self.event.currentIndex()>=0 and self.event.currentText()==self.event.itemText(self.event.currentIndex()) else self.event.currentText()
        args = (dirs,self.query.text().strip(),event or '',self.after.text().strip(),self.before.text().strip(),self.page)
        self.status.setText('正在查找全部历史分卷…'); self.previous.setEnabled(False); self.next.setEnabled(False)
        def work():
            result = search_logs(*args,cancelled=lambda:token!=self._token)
            try: self.loaded.emit(token,result)
            except RuntimeError: pass
        threading.Thread(target=work,daemon=True,name='review-log-search').start()

    def show_result(self,token,result):
        if token!=self._token: return
        rows,total,errors = result
        self.output.setPlainText('\n'.join(rows))
        self.status.setText(f'匹配 {total} 条｜第 {self.page+1} 页，每页最多300条。'+('；'.join(errors) if errors else ''))
        self.previous.setEnabled(self.page>0); self.next.setEnabled((self.page+1)*300<total)

    def closeEvent(self,event):
        self._token += 1
        super().closeEvent(event)
