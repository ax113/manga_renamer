"""A shared persisted archive switch for all RESULT import entrances."""
import copy
import json
from pathlib import Path
from PySide6.QtWidgets import QWidget,QHBoxLayout,QCheckBox,QPushButton,QFileDialog,QMessageBox
from .review_ui import address_field,set_address
from .ai_txt import atomic_text


class ArchiveControls(QWidget):
    def __init__(self,owner):
        super().__init__()
        self.owner = owner
        row = QHBoxLayout(self); row.setContentsMargins(0,0,0,0)
        self.enabled = QCheckBox('导入后自动归档'); row.addWidget(self.enabled)
        self.directory = address_field(); row.addWidget(self.directory,1)
        self.change = QPushButton('更改归档目录'); row.addWidget(self.change)
        self.enabled.toggled.connect(self.toggle); self.change.clicked.connect(self.choose)
        self.refresh()

    def refresh(self):
        self.enabled.blockSignals(True); self.enabled.setChecked(bool(self.owner.settings_data.get('txt_archive_enabled',False)))
        self.enabled.blockSignals(False)
        set_address(self.directory,self.owner.settings_data.get('txt_archive_directory',''),'尚未设置归档目录')

    def _save(self,**values):
        settings = copy.deepcopy(self.owner.settings_data); settings.update(values)
        try: atomic_text(self.owner.settings_path(),json.dumps(settings,ensure_ascii=False,indent=2))
        except OSError as exc:
            QMessageBox.warning(self,'归档设置未保存',str(exc)); self.refresh(); return False
        self.owner.settings_data = settings
        self.owner._refresh_ai_dialog()
        self.refresh(); return True

    def choose(self):
        start = self.owner.settings_data.get('txt_archive_directory') or str(Path(self.owner.settings_data.get('txt_result_directory') or '.'))
        directory = QFileDialog.getExistingDirectory(self,'选择RESULT归档目录',start)
        return self._save(txt_archive_directory=directory) if directory else False

    def toggle(self,enabled):
        if enabled and not self.owner.settings_data.get('txt_archive_directory') and not self.choose():
            self.refresh(); return
        self._save(txt_archive_enabled=enabled)
