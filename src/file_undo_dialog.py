"""Compact confirmation for independent name/location restoration."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QCheckBox, QPushButton,
    QTableWidgetItem,
)
from .file_review_ui import FileTable


class UndoDialog(QDialog):
    def __init__(self, parent, plans, problems, action,
                 combined_plans=None, combined_problems=None):
        super().__init__(parent)
        self.plans, self.problems, self.action = plans, problems, action
        self.combined_plans = combined_plans
        self.combined_problems = combined_problems
        self.setWindowTitle('确认移回原处' if action == 'move' else '确认恢复原名')
        self.resize(560, 150)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.summary.setOpenExternalLinks(False)
        layout.addWidget(self.summary, 0, Qt.AlignTop)
        self.include_moves = QCheckBox('同时移回改名前位置')
        self.include_moves.setChecked(False)
        self.include_moves.setVisible(combined_plans is not None)
        layout.addWidget(self.include_moves)
        self.details = FileTable(
            ['漫画', '处理方式', '当前位置', '恢复后路径', '原因'],
            (200, 220, 300, 300, 300),
        )
        self.details.hide()
        layout.addWidget(self.details, 1)
        self.summary.linkActivated.connect(self.toggle_details)
        row = QHBoxLayout()
        row.addStretch()
        self.start, self.cancel = QPushButton(), QPushButton('取消')
        self.start.clicked.connect(self.accept)
        self.cancel.clicked.connect(self.reject)
        self.cancel.setDefault(True)
        row.addWidget(self.start)
        row.addWidget(self.cancel)
        layout.addLayout(row)
        self.include_moves.toggled.connect(self.update_counts)
        self.update_counts()

    def selected_plans(self):
        if self.include_moves.isChecked() and self.combined_plans is not None:
            return self.combined_plans
        return self.plans

    def selected_problems(self):
        if self.include_moves.isChecked() and self.combined_plans is not None:
            return self.combined_problems
        return self.problems

    def update_counts(self, *_):
        plans, problems = self.selected_plans(), self.selected_problems()
        safe = [p for p in plans if not problems[p['parent_row']]]
        blocked = len(plans) - len(safe)
        combined = self.include_moves.isChecked() and self.combined_plans is not None
        if self.action == 'move':
            description = f'将 <b>{len(safe)} 本</b>移回本次移动前的位置，保留当前名称。'
            button = '移回原处'
        elif combined:
            description = f'将 <b>{len(safe)} 本</b>移回改名前的位置，并恢复原名。'
            button = '恢复原名并移回'
        else:
            description = f'在当前位置恢复 <b>{len(safe)} 本</b>改名前的名称。'
            button = '恢复原名'
        self.summary.setText(
            f'{description}<br>无法执行：{blocked} 本。'
            '<a href="details">查看明细</a>'
        )
        self.start.setText(f'{button} {len(safe)} 本')
        self.start.setEnabled(bool(safe))
        self.details.setRowCount(len(plans))
        for n, plan in enumerate(plans):
            problem = problems[plan['parent_row']]
            method = '无法执行' if problem else button
            values = (plan['current_name'], method, plan['source_path'],
                      plan['target_path'], problem or '-')
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                item.setToolTip(str(value) + '\n右键复制完整内容；双击可拖选部分文字复制。')
                self.details.setItem(n, col, item)

    def toggle_details(self, *_):
        expanded = not self.details.isVisible()
        self.details.setVisible(expanded)
        self.layout().activate()
        if expanded:
            self.resize(800, 420)
        else:
            self.adjustSize()
            self.resize(560, max(150, self.minimumSizeHint().height()))
