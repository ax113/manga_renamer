"""Explicit scopes, centered enable checks and a scoped match preview."""
import copy
import uuid
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QDialog,QVBoxLayout,QHBoxLayout,QLabel,QTableWidgetItem,
    QPushButton,QMessageBox,QAbstractItemView,QHeaderView,QComboBox,QTreeWidget,QTreeWidgetItem,QPlainTextEdit)
from .ui_common import AlignedComboBox
from .ai_supplements import validate_supplements, matching_supplements
from .review_ui import ComicTable, ReviewTree


SUPPLEMENT_HELP = '''补充依据怎么填写

用途
这里填写你已经核实、但漫画名称和数据库资料里没有说清的事实。API和TXT新任务都会带上适用的依据，帮助AI判断。
例如：本地收录到第几话、是否仍在连载、作者原名或汉化来源。没有额外事实时可以不填；保存前删除空白行。

操作顺序
1. 点“添加”，每条依据占一行。
2. 选择“适用范围”，填写“匹配文字”和“判断依据”。
3. 点“预览匹配”，检查命中了哪些漫画。
4. 勾选需要启用的条目，点“保存”，然后新建API或TXT任务。

适用范围与匹配文字
“包含指定文字”：只对资料中包含该文字的漫画使用这条依据。匹配范围包括送审的本地名称、路径和数据库来源资料；不区分大小写，不是正则表达式。
“全部送审漫画”：对本次送审范围中的每一本使用；匹配文字可以留空，不代表自动扩大到整个库。
同一格填“A,B”会按完整的“A,B”查找，逗号不是“或者”。不同匹配条件请分行添加。

可以照填的例子
适用范围：包含指定文字
匹配文字：作品完整标题（换成你要匹配的实际标题）
判断依据：已逐页核对，本地只收录第1～5话，作品尚未完结，请保留话数范围和进行中标记。

判断依据应写已核实的事实，有来源时补上来源。只写“名字不对，帮我改一下”不能提供可靠证据。匹配越宽，命中的漫画可能越多，先预览确认。

启用与预览
“启用”列勾选表示新任务会使用该条；取消勾选可暂时停用。点击“启用”表头可以全部启用或全部取消。
预览使用当前表格里尚未保存的填写内容；停用条目也会显示潜在匹配，并标注“停用（不送审）”。
从API／TXT页打开，预览各页当前选择的送审范围；从日志页打开，预览所选任务的漫画。预览只检查匹配范围，不修改原任务，也不保证事实或AI判断正确。

保存以后何时生效
新建任务时保存依据快照。之后修改、停用或删除依据，都不改变已创建任务；原任务继续复核、重试或重新导出仍使用原快照。
要使用新依据，请保存后新建任务。TXT依据已经放进INPUT，不需要另外给审核对话复制一次。
补充依据不解除人工名称保护，也不替代通用审核规则。证据有实质冲突时仍需人工判断。
'''


class SupplementDialog(QDialog):
    def __init__(self,owner,parent=None):
        super().__init__(parent)
        self.owner = owner
        self.setWindowTitle('共用补充判断依据'); self.resize(880,510)
        root = QVBoxLayout(self)
        label = QLabel('示例：添加按钮→范围选包含指定文字→匹配文字写要筛的字段→判断依据用自然语言简短的写清楚需求即可→预览匹配→启用并保存 保存后对新建的API/TXT任务生效。具体使用方法请看填写说明。')
        label.setWordWrap(True)
        intro = QHBoxLayout(); intro.addWidget(label, 1)
        self.help_btn = QPushButton('填写说明'); self.help_btn.clicked.connect(self.show_help)
        intro.addWidget(self.help_btn, 0, Qt.AlignTop); root.addLayout(intro)
        self.table = ComicTable(); self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(['启用','适用范围','匹配文字','判断依据'])
        self.table.enable_select_all_header()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Interactive)
        self.table.setColumnWidth(3,360)
        self.table.setColumnWidth(0,self.table.fontMetrics().horizontalAdvance('启用')+34); self.table.setColumnWidth(1,150); self.table.setColumnWidth(2,210)
        self.table._initial_widths = True
        self.table.horizontalHeaderItem(0).setToolTip('点击全部启用 / 全部取消启用')
        self.table.horizontalHeader().sectionClicked.connect(self.enable_all)
        root.addWidget(self.table,1)
        for row in copy.deepcopy(owner.settings_data.get('ai_supplements',[])): self.add_row(row)
        buttons = QHBoxLayout()
        add = QPushButton('添加'); add.clicked.connect(lambda:self.add_row())
        remove = QPushButton('删除选中'); remove.clicked.connect(self.remove_rows)
        self.preview_btn = QPushButton('预览匹配'); self.preview_btn.clicked.connect(self.preview)
        save = QPushButton('保存'); save.clicked.connect(self.save)
        cancel = QPushButton('取消'); cancel.clicked.connect(self.reject)
        for w in (add,remove,self.preview_btn): buttons.addWidget(w)
        buttons.addStretch()
        for w in (save,cancel): buttons.addWidget(w)
        root.addLayout(buttons)

    def show_help(self):
        dialog = QDialog(self); dialog.setWindowTitle('补充依据填写说明'); dialog.resize(700,520)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(); text.setReadOnly(True); text.setPlainText(SUPPLEMENT_HELP)
        layout.addWidget(text,1)
        close = QPushButton('关闭'); close.clicked.connect(dialog.accept); layout.addWidget(close)
        dialog.exec()

    def add_row(self,row=None):
        row = row or {'id':uuid.uuid4().hex,'enabled':True,'match':'','text':'','scope':'contains'}
        index = self.table.rowCount(); self.table.insertRow(index); self.table.setRowHeight(index,65)
        enabled = QTableWidgetItem(); enabled.setFlags(Qt.ItemIsEnabled|Qt.ItemIsSelectable|Qt.ItemIsUserCheckable)
        enabled.setCheckState(Qt.Checked if row.get('enabled',True) else Qt.Unchecked)
        enabled.setData(Qt.UserRole,row.get('id') or uuid.uuid4().hex)
        self.table.setItem(index,0,enabled)
        scope = AlignedComboBox(); scope.addItem('包含指定文字','contains'); scope.addItem('全部送审漫画','all')
        scope.setCurrentIndex(scope.findData(row.get('scope','contains' if row.get('match') else 'all')))
        scope.setStyleSheet('QComboBox { background: transparent; border: none; margin: 1px; }')
        self.table.setCellWidget(index,1,scope)
        for col,key in [(2,'match'),(3,'text')]: self.table.setItem(index,col,QTableWidgetItem(row.get(key,'')))
        self.table.scrollToItem(enabled)

    def enable_all(self,column):
        if column!=0: return
        checked = not all(self.table.item(i,0).checkState()==Qt.Checked for i in range(self.table.rowCount()))
        for i in range(self.table.rowCount()): self.table.item(i,0).setCheckState(Qt.Checked if checked else Qt.Unchecked)

    def remove_rows(self):
        for row in sorted({index.row() for index in self.table.selectedIndexes()},reverse=True): self.table.removeRow(row)

    def rows(self):
        return [{'id':self.table.item(i,0).data(Qt.UserRole),'enabled':self.table.item(i,0).checkState()==Qt.Checked,
                 'scope':self.table.cellWidget(i,1).currentData(),'match':self.table.item(i,2).text(),
                 'text':self.table.item(i,3).text()} for i in range(self.table.rowCount())]

    def preview_items(self):
        parent = self.parentWidget()
        if parent and hasattr(parent,'tabs'):
            if parent.tabs.currentIndex()==0:
                return self.owner.ai_items_for_scope(parent.scope.currentData(),parent._category_choices,parent._prepared_ids), 'API页当前送审范围'
            if parent.tabs.currentIndex()==1:
                page = parent.txt_page
                return self.owner.ai_items_for_scope(page.scope.currentData(),page.categories), 'TXT页当前送审范围'
            entry = parent.task_list.currentItem()
            task = self.owner.review_task(entry.data(Qt.UserRole)) if entry else None
            if task:
                items = self.owner._txt_context(task['task_id']).items
                return [item for item in items if item.local_id in task['items']], '所选任务的漫画（仅预览，不改任务快照）'
        return self.owner.ai_items_for_scope('all'), '全部漫画（未指定送审范围，非本次任务数量）'

    def match_preview(self):
        from .ai_review import business_input
        rows = validate_supplements(self.rows())
        items,label = self.preview_items()
        facts = [(item,business_input(item)) for item in items]
        return label,len(items),[(row,[item for item,fact in facts if matching_supplements(fact,[dict(row,enabled=True)])]) for row in rows]

    def preview(self):
        try: label,total,rows = self.match_preview()
        except (ValueError,OSError) as exc:
            QMessageBox.warning(self,'无法预览',str(exc)); return
        dialog = QDialog(self); dialog.setWindowTitle('补充依据匹配预览'); dialog.resize(760,480)
        root = QVBoxLayout(dialog); root.addWidget(QLabel(f'统计范围：{label}｜共{total}本'))
        tree = ReviewTree(); tree.setHeaderLabels(['依据 / 漫画','匹配本数 / 路径'])
        for number,(row,matched) in enumerate(rows,1):
            node = QTreeWidgetItem(tree,[f"{number}｜{'启用' if row['enabled'] else '停用（不送审）'}｜{row['text']}",str(len(matched))])
            for item in matched: QTreeWidgetItem(node,[item.original_name,item.original_path])
        root.addWidget(tree,1); close = QPushButton('关闭'); close.clicked.connect(dialog.accept); root.addWidget(close)
        dialog.exec()

    def save(self):
        try: self.owner.save_ai_supplements(validate_supplements(self.rows()))
        except (ValueError,OSError) as exc:
            QMessageBox.warning(self,'补充依据未保存',str(exc)); return
        self.accept()
