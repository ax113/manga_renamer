"""API/TXT review forms and task-bound remaining comic selection."""

from __future__ import annotations

import copy
import json
import threading
from time import perf_counter

from PySide6.QtCore import Qt, Signal, QEvent, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit, QPushButton,
    QTabWidget, QVBoxLayout, QWidget, QSplitter, QCheckBox, QTableWidgetItem, QMenu, QSizePolicy,
)

from .ai_transport import ApiError, post_completion, validate_config
from .ai_config import PROVIDERS, THINKING_MODES, provider_for, thinking_mode, RUNTIME_DEFAULTS, EFFORTS, effort_choices, runtime_values, default_thinking_label, default_effort_label, automatic_provider, default_max_tokens, parse_custom_parameters
from .review_ui import set_address
from .ai_supplements import enabled_supplements
from .ai_cost import price_preset, cost_line, preset_state
from .ai_review import ReviewResponseError, parse_response_content
from .models import CATEGORIES
from .ai_log import TaskLog
from .ai_usage import usage_lines
from .ai_txt_page import TxtPage, TASK_LABELS
from .ai_txt import counts, split_sizes, ROW_LABELS
from .ai_txt_store import is_txt, accessible
from .ai_remaining import remaining_status
from .run_log import register_secret, _redact
from pathlib import Path
import uuid


class AiDialog(QDialog):
    checkFinished = Signal(bool, str)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.setWindowTitle("AI复核｜API / TXT")
        available = self.screen().availableGeometry()
        self.resize(min(850,max(520,available.width()-40)), min(700,max(520,available.height()-48)))
        self.setAttribute(Qt.WA_DeleteOnClose, False)
        self._prepared_ids: tuple[str, ...] | None = None
        self._category_choices: list[str] = []
        self._config_loaded = False
        self._editing_profile_id = ""
        self._baseline_config = {}
        self._testing = False

        from .ai_dialog_layout import build
        perf = getattr(owner, '_perf_diag', None)
        started = perf_counter() if perf else 0
        build(self)
        if perf:
            perf.mark('ai.dialog.build', started)
        self._review_page_index = self.tabs.currentIndex()
        self.tabs.currentChanged.connect(self._page_changed)
        self.checkFinished.connect(self._check_finished)
        self.refresh()

    def closeEvent(self, event):
        if not self._guard_changes('关闭'):
            event.ignore()
            return
        self._lock_editor()
        self._save_comic_column_widths()
        self.hide()  # Closing this window never interrupts the background task.
        event.ignore()

    def reject(self):
        # Escape must use the same edit protection as the window close button.
        self.close()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    def _page_changed(self, index):
        previous = self._review_page_index
        if previous == 0 and index != 0 and self._editor_unlocked:
            blocked = self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(previous)
            self.tabs.blockSignals(blocked)
            if not self._guard_changes('切换'):
                return
            blocked = self.tabs.blockSignals(True)
            self.tabs.setCurrentIndex(index)
            self.tabs.blockSignals(blocked)
        self._review_page_index = self.tabs.currentIndex()

    def show_settings(self):
        self.tabs.setCurrentIndex(0)
        self._unlock_editor()
        self.show()
        self.raise_()

    def prepare_checked(self, ids: tuple[str, ...]):
        self._prepared_ids = ids
        self.scope.setCurrentIndex(0)
        self.refresh()

    def _choose_categories(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("指定分类")
        layout = QVBoxLayout(dialog)
        choices = QListWidget()
        for category in CATEGORIES:
            entry = QListWidgetItem(category)
            entry.setFlags(entry.flags() | Qt.ItemIsUserCheckable)
            entry.setCheckState(Qt.Checked if category in self._category_choices else Qt.Unchecked)
            choices.addItem(entry)
        layout.addWidget(choices)
        ok = QPushButton("确定")
        ok.clicked.connect(dialog.accept)
        layout.addWidget(ok)
        if dialog.exec() == QDialog.Accepted:
            self._category_choices = [choices.item(i).text() for i in range(choices.count()) if choices.item(i).checkState() == Qt.Checked]
            self._update_scope_count()

    def _update_scope_count(self):
        scope = self.scope.currentData()
        self.choose_category_btn.setEnabled(scope == "categories")
        search_index = self.scope.findData("search")
        self.scope.model().item(search_index).setEnabled(True)
        count = len(self.owner.ai_items_for_scope(scope, self._category_choices, self._prepared_ids))
        self.count_label.setText(f"本次范围：{count} 本｜启用补充依据 {len(enabled_supplements(self.owner.settings_data))} 条" + ("；分类可多选" if scope == "categories" else ""))

    def _start(self):
        scope = self.scope.currentData()
        ids = self._prepared_ids if scope == "checked" else None
        self.owner.start_api_from_scope(scope, self._category_choices, ids)
        self._prepared_ids = None
        self.refresh()

    def _provider_config(self):
        return {"endpoint": self.endpoint.text().strip(), "model": self.model.text().strip(),
                "provider": self.provider.currentData(), "thinking_mode": self.thinking.currentData()}

    def _form_config(self):
        values = {}
        for key, edit in self.runtime_edits.items():
            text = edit.text().strip()
            if not text.isascii() or not text.isdigit():
                raise ValueError("高级参数必须填写整数；重试次数可为0，其余应大于0")
            values[key] = int(text)
        runtime_values(values)
        return {"name": self.profile_name.text().strip(), "key": self.key.text().strip(),
                **self._provider_config(), **values, "effort":self.effort.currentData(),
                "pricing":self._form_pricing(), "max_tokens":self._form_max_tokens() if automatic_provider(self._provider_config())!="generic" else default_max_tokens(self._provider_config()),
                "custom_parameters":parse_custom_parameters(self.custom_parameters.toPlainText()) if automatic_provider(self._provider_config())=="generic" else {},
                "price_preset_id":self.price_kind.currentData(), "price_presets":self._preset_rows()}

    def _set_max_tokens(self, text):
        index = self.max_tokens.findData(int(text))
        if index >= 0: self.max_tokens.setCurrentIndex(index)
        else: self.max_tokens.setCurrentText(str(text))

    def _form_max_tokens(self):
        text = self.max_tokens.currentText().strip().split('/')[-1]
        if not text.isascii() or not text.isdigit() or int(text)<1:
            raise ValueError('最大Token必须为正整数')
        return int(text)

    def _form_pricing(self):
        if self.price_kind.currentData() == 'none':
            return {}
        tariff = copy.deepcopy(self._price_snapshot)
        tariff['currency'] = self.currency.currentData()
        tariff['label'] = self.price_name.text().strip()
        index = min(self._price_tier_index,len(tariff['tiers'])-1)
        tariff['tiers'][index].update({k:w.text().strip() for k,w in self.price_edits.items()})
        return tariff

    def _preset_rows(self):
        rows = copy.deepcopy(self._price_presets)
        for row in rows:
            if row['id'] == self._price_selected_kind:
                row.update(name=self.price_name.text().strip(),pricing=self._form_pricing())
        return rows

    def _populate_price_presets(self, selected='none'):
        self.price_kind.blockSignals(True)
        self.price_kind.clear(); self.price_kind.addItem('未配置价格','none')
        for row in self._price_presets: self.price_kind.addItem(row['name'],row['id'])
        self.price_kind.setCurrentIndex(max(0,self.price_kind.findData(selected)))
        self.price_kind.blockSignals(False)

    def _load_pricing(self, tariff, preset_id=''):
        if not tariff:
            kind = 'none'
        else:
            kind = preset_id if any(r['id']==preset_id for r in self._price_presets) else ''
            if not kind:
                kind = next((r['id'] for r in self._price_presets if r['pricing']==tariff), '')
            if not kind:
                # Saved configuration rates survive deletion of a shared preset.
                kind = 'profile_price'
        self._populate_price_presets(kind)
        if kind == 'profile_price':
            self.price_kind.blockSignals(True)
            self.price_kind.addItem(tariff.get('label') or '本配置费率',kind)
            self.price_kind.setCurrentIndex(self.price_kind.findData(kind))
            self.price_kind.blockSignals(False)
        self._price_snapshot = copy.deepcopy(tariff)
        self._price_selected_kind = kind; self._price_tier_index = 0
        self.price_name.setText(tariff.get('label',''))
        row = next((r for r in self._price_presets if r['id']==kind),None)
        if row: self.price_name.setText(row['name'])
        if self._price_snapshot: self._price_snapshot['label'] = self.price_name.text()
        self.currency.setCurrentIndex(max(0,self.currency.findData(tariff.get('currency','CNY'))))
        self.price_tier.blockSignals(True); self.price_tier.clear()
        for i,tier in enumerate(tariff.get('tiers',[])):
            label = f"第{i+1}档：输入≤{tier['max_input']:,}" if tariff.get('kind')=='tiered' else '高峰价（空闲半价）' if tariff.get('kind')=='ds_peak' else '固定价格'
            self.price_tier.addItem(label,i)
        self.price_tier.blockSignals(False)
        tier = (tariff.get('tiers') or [{}])[0]
        for k,w in self.price_edits.items(): w.setText(str(tier.get(k,'')))
        self._price_widgets()

    def _price_widgets(self):
        configured = self.price_kind.currentData() != 'none'
        for w in [self.currency,self.price_name,self.price_tier,*self.price_edits.values()]:
            w.setEnabled(self._editor_unlocked and configured)
        self.new_price_btn.setEnabled(self._editor_unlocked)
        self.delete_price_btn.setEnabled(self._editor_unlocked and configured)
        self.price_panel.layout().setRowVisible(self.price_tier,False)
        legacy = len(self._price_snapshot.get('tiers',[])) > 1 or self._price_snapshot.get('kind')=='ds_peak'
        if legacy:
            for w in self.price_edits.values(): w.setEnabled(False)
            self.price_name.setToolTip('旧配置费用快照保留；选择独立档位或峰/谷预设后可编辑固定费率。')
        else: self.price_name.setToolTip('')
        self.price_hint.clear(); self.price_hint.hide()
        self.price_status.clear(); self.price_status.hide()
        self.price_summary.clear(); self.price_summary.hide()

    def _update_price_summary(self, *_args):
        # The requested redundant description rows are removed.
        self.price_summary.clear()

    def _change_price_kind(self):
        if not hasattr(self,'_price_presets'): return
        selected = self.price_kind.currentData()
        # Remember edits to the previous preset when selecting another.
        for row in self._price_presets:
            if row['id']==self._price_selected_kind:
                current = self.price_kind.currentIndex()
                self.price_kind.blockSignals(True)
                self.price_kind.setCurrentIndex(self.price_kind.findData(self._price_selected_kind))
                row.update(name=self.price_name.text().strip(),pricing=self._form_pricing())
                self.price_kind.setCurrentIndex(current); self.price_kind.blockSignals(False)
        chosen = next((r for r in self._price_presets if r['id']==selected),None)
        self._load_pricing(chosen['pricing'] if chosen else {},selected)
        self.config_note.clear()

    def _change_price_tier(self, index):
        if not hasattr(self,'_price_tier_index') or index<0: return
        self._price_snapshot = self._form_pricing()
        self._price_tier_index = index
        tier = self._price_snapshot['tiers'][index]
        for k,w in self.price_edits.items(): w.setText(str(tier.get(k,'')))

    def _new_price_preset(self):
        self._price_presets = self._preset_rows()
        names = {r['name'] for r in self._price_presets}; number = 1
        while f'新预设 {number}' in names: number += 1
        name = f'新预设 {number}'
        row = {'id':uuid.uuid4().hex,'name':name,'pricing':{'kind':'flat','currency':'CNY',
               'label':name,'tiers':[{'max_input':2147483647,'input':'0','cached':'0','output':'0'}]}}
        self._price_presets.append(row)
        self._load_pricing(row['pricing'],row['id'])
        self.price_name.setFocus(); self.price_name.selectAll()

    def _delete_price_preset(self):
        selected = self.price_kind.currentData()
        self._price_presets = [r for r in self._price_presets if r['id']!=selected]
        self._load_pricing({})

    def _update_price_choices(self):
        if hasattr(self,'price_status'):
            self.price_status.clear(); self.price_status.hide()

    def _quick_fill(self,index):
        key = self.quick_fill.itemData(index)
        connections = {'deepseek':('https://api.deepseek.com','deepseek-flash','deepseek'),
            'qwen':('https://maas.qianwenaiapi.com/compatible-mode/v1','qwen3.7-flash','qwen'),
            'qwen_intl':('https://dashscope-intl.aliyuncs.com/compatible-mode/v1','qwen3.7-flash','qwen'),
            'openai':('https://api.openai.com/v1','','generic'),
            'siliconflow':('https://api.siliconflow.cn/v1','','generic'),
            'openrouter':('https://openrouter.ai/api/v1','','generic')}
        if key not in connections or not self._editor_unlocked: return
        endpoint,model,provider = connections[key]
        self.endpoint.setText(endpoint); self.model.setText(model)
        self.provider.setCurrentIndex(self.provider.findData(provider))
        self.thinking.setCurrentIndex(self.thinking.findData('default'))
        self.effort.setCurrentIndex(self.effort.findData('default'))
        self._set_max_tokens(str(default_max_tokens(self._provider_config())))
        self.custom_parameters.clear()
        self.quick_fill.setCurrentIndex(0)

    def _editor_widgets(self):
        return [self.profile_name,self.endpoint,self.key,self.model,self.provider,self.thinking,
                self.effort,self.quick_fill,self.max_tokens,self.custom_parameters,*self.runtime_edits.values(),self.restore_btn,self.price_kind,self.save_btn,
                self.cancel_btn,self.test_btn,self.delete_profile_btn]

    def _lock_editor(self):
        self._editor_unlocked = False
        for w in self._editor_widgets(): w.setEnabled(False)
        self.edit_btn.setEnabled(True)
        self._price_widgets()
        self._update_provider_hint()

    def _unlock_editor(self):
        self._editor_unlocked = True
        for w in self._editor_widgets(): w.setEnabled(True)
        self.delete_profile_btn.setEnabled(bool(self._editing_profile_id))
        self.test_btn.setEnabled(not self._testing)
        self.edit_btn.setEnabled(False)
        self._price_widgets()
        self._update_provider_hint()

    def _cancel_config(self):
        self._load_profile(self.owner.current_api_config())
        self.config_note.clear()

    def _restore_runtime(self):
        for key,value in RUNTIME_DEFAULTS.items(): self.runtime_edits[key].setText(str(value))
        self.thinking.setCurrentIndex(self.thinking.findData('default'))
        self.effort.setCurrentIndex(self.effort.findData('default'))
        self._set_max_tokens(str(default_max_tokens(self._provider_config())))
        self.config_note.setText('已恢复推荐运行参数，仅修改表单；接口、密钥、模型及费用价格保留。保存后新任务才使用。')

    def _edit_supplements(self):
        from .ai_supplement_dialog import SupplementDialog
        dialog = SupplementDialog(self.owner,self)
        dialog.exec()
        self.refresh()

    def _api_to_txt(self):
        entry = self.task_list.currentItem()
        if not entry:
            return
        task_id = entry.data(Qt.UserRole)
        task = self.owner.review_task(task_id)
        selected = self._checked_comics()
        mode, value = self.txt_page.mode.currentData(), self.txt_page.value.value()
        if is_txt(task):
            ids = selected or [i for i, status in self._comic_status.items() if status == 'ready']
            self.owner.supplement_txt(task_id, ids, mode, value)
        else:
            self.owner.api_remaining_to_txt(task_id, mode, value, selected=selected or None)

    def _view_logs(self):
        from .ai_log_viewer import LogViewer
        entry = self.task_list.currentItem()
        viewer = LogViewer(self.owner,entry.data(Qt.UserRole) if entry else "",self)
        viewer.exec()

    def _copy_diagnostics(self):
        from .ai_log_viewer import diagnostic_text
        from PySide6.QtWidgets import QApplication
        entry = self.task_list.currentItem()
        if entry:
            QApplication.clipboard().setText(diagnostic_text(self.owner.review_task(entry.data(Qt.UserRole))))
            self.owner._show_status_message('本任务脱敏诊断已复制，可直接粘贴。',4000)

    def _load_profile(self, profile: dict):
        self._editing_profile_id = profile.get("id", "")
        self.profile_name.setText(str(profile.get("name", "")))
        self.endpoint.setText(str(profile.get("endpoint", "")))
        self.key.setText(str(profile.get("key", "")))
        self.model.setText(str(profile.get("model", "")))
        self.provider.setCurrentIndex(max(0, self.provider.findData(provider_for(profile))))
        self.thinking.setCurrentIndex(max(0, self.thinking.findData(thinking_mode(profile))))
        for key,default in RUNTIME_DEFAULTS.items(): self.runtime_edits[key].setText(str(profile.get(key,default)))
        self.effort.setCurrentIndex(max(0,self.effort.findData(profile.get('effort','default'))))
        self._price_presets = preset_state(self.owner.settings_data)
        self._set_max_tokens(str(profile.get('max_tokens',default_max_tokens(profile))))
        self.custom_parameters.setPlainText(json.dumps(profile.get('custom_parameters',{}),ensure_ascii=False,indent=2) if profile.get('custom_parameters') else '')
        self._load_pricing(profile.get('pricing',{}),profile.get('price_preset_id',''))
        self._baseline_config = dict(self._form_config(), id=self._editing_profile_id)
        self._populate_profiles()
        self._lock_editor()

    def _populate_profiles(self):
        profiles, _ = self.owner.api_profiles()
        self.profiles.blockSignals(True)
        self.profiles.clear()
        for profile in profiles:
            self.profiles.addItem(_redact(profile["name"]), profile["id"])
        if not self._editing_profile_id:
            self.profiles.addItem("新配置（未保存）", "")
        self.profiles.setCurrentIndex(self.profiles.findData(self._editing_profile_id))
        self.profiles.blockSignals(False)
        self.delete_profile_btn.setEnabled(bool(self._editing_profile_id))

    def _update_provider_hint(self):
        self.thinking.setItemText(self.thinking.findData('default'), default_thinking_label(self._provider_config()))
        self.effort.setItemText(self.effort.findData('default'), default_effort_label(self._provider_config()))
        self.effort.setToolTip('括号说明当前模式下的实际默认强度；模型内置表示未提供可选强度档位。第三方渠道和未知模型不推断。')
        self.thinking.setToolTip('接口默认不发送开关参数；括号表示已核验的官方模型默认值。第三方渠道和未核验模型请自行确认。')
        try:
            provider = automatic_provider(self._provider_config())
        except ValueError:
            provider = "generic"
        for mode in ("enabled", "disabled"):
            self.thinking.model().item(self.thinking.findData(mode)).setEnabled(provider != "generic")
        supported = effort_choices(self._provider_config())
        for i in range(self.effort.count()): self.effort.model().item(i).setEnabled(self.effort.itemData(i) in supported)
        if self.effort.currentData() not in supported:
            self.effort.setCurrentIndex(self.effort.findData('default'))
        self.max_tokens.setEnabled(self._editor_unlocked and provider!='generic')
        self.custom_parameters.setEnabled(self._editor_unlocked and provider=='generic')
        self.custom_toggle.setEnabled(self._editor_unlocked and provider=='generic')
        self.effort.setEnabled(self._editor_unlocked and self.thinking.currentData()!='disabled' and (len(supported)>1 or self.effort.currentData()!='default'))
        self.thinking.setEnabled(self._editor_unlocked and provider!='generic')
        self.max_tokens.setToolTip('最大输出Token；Qwen内置参数限制思考与回复的总输出，自定义同名参数优先。')
        self._update_price_choices()

    def _guard_changes(self, action='切换') -> bool:
        if not self._editor_unlocked:
            baseline = {key:value for key,value in self._baseline_config.items() if key != 'id'}
            try:
                if self._form_config() == baseline:
                    return True
            except ValueError:
                pass
        box = QMessageBox(self)
        box.setWindowTitle("配置尚未保存")
        box.setText(f"API配置仍在编辑。{action}前如何处理？")
        save = box.addButton("保存并" + action, QMessageBox.AcceptRole)
        discard = box.addButton("放弃修改并" + action, QMessageBox.DestructiveRole)
        cancel = box.addButton("取消", QMessageBox.RejectRole)
        box.setDefaultButton(cancel)
        box.setEscapeButton(cancel)
        box.exec()
        if box.clickedButton() is save:
            return self._save_config()
        if box.clickedButton() is discard:
            self._load_profile(self._baseline_config)
            return True
        return False

    def _select_profile(self, index: int):
        target = self.profiles.itemData(index)
        if target is None or target == self._editing_profile_id:
            return
        if not self._guard_changes():
            self._populate_profiles()
            return
        try:
            self.owner.select_api_profile(target)
        except Exception as exc:
            QMessageBox.warning(self, "配置未切换", str(exc))
            self._populate_profiles()
            return
        self._load_profile(self.owner.current_api_config())
        self.config_note.setText("以后新建的任务使用这套已保存配置。正在运行的任务继续使用原配置。")

    def _unique_name(self, stem: str) -> str:
        names = {p["name"].casefold() for p in self.owner.api_profiles()[0]}
        value, number = stem[:75], 2
        while value.casefold() in names:
            value = stem[:70] + f" {number}"
            number += 1
        return value

    def _new_profile(self):
        if self._guard_changes():
            self._load_profile({"name": self._unique_name("新配置"), "provider": "auto", "thinking_mode": "default"})
            self._unlock_editor()
            self.config_note.setText("新配置尚未保存。填好后点“保存并使用”。")

    def _copy_profile(self):
        if self._guard_changes():
            profile = self._form_config()
            profile["name"] = self._unique_name((profile["name"] or "配置") + " 副本")
            self._load_profile(profile)
            self._unlock_editor()
            self.config_note.setText("副本尚未保存，可独立修改模型或推理模式，再点“保存并使用”。")

    def _delete_profile(self):
        if not self._guard_changes() or not self._editing_profile_id:
            return
        choice = QMessageBox.question(self, "删除配置", "删除这套已保存配置？正在运行的任务和历史任务记录会保留。",
                                      QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if choice != QMessageBox.Yes:
            return
        try:
            self.owner.delete_api_profile(self._editing_profile_id)
        except Exception as exc:
            QMessageBox.warning(self, "配置未删除", str(exc))
            return
        self._load_profile(self.owner.current_api_config())
        self.config_note.setText("配置已删除。")

    def _save_config(self) -> bool:
        try:
            profile_id = self.owner.save_api_config(self._form_config(), self._editing_profile_id)
        except Exception as exc:
            self.config_note.setText(_redact(str(exc)))
            QMessageBox.warning(self, "配置未保存", str(exc))
            return False
        self._load_profile(self.owner.current_api_config())
        self.config_note.setText("配置已保存并选用。以后新建的任务使用这一份配置。")
        return bool(profile_id)

    def _test_config(self):
        try:
            config = self._form_config()
            validate_config(config)
        except ValueError as exc:
            self.config_note.setText(str(exc))
            return
        if self._testing:
            return
        self._testing = True
        self.test_btn.setEnabled(False)
        self.config_note.setText("正在测试当前表单…")
        register_secret(config["key"])
        label = _redact(config.get("name") or "未命名配置")
        context = _redact(f"配置 {label}｜模型 {config['model']}｜推理 {THINKING_MODES[config['thinking_mode']]}：")
        warnings = []
        audit = TaskLog(self.owner.review_log_root(), "test_" + uuid.uuid4().hex,
                        lambda warning: warnings.append(warning["message"]))
        def work():
            try:
                response = post_completion(config, [], timeout=config["timeout_seconds"], audit=audit)
                parse_response_content(response)
                self.checkFinished.emit(True, context + "接口可用。测试不会保存设置。" + ("；" + "；".join(warnings) if warnings else ""))
            except Exception as exc:
                # Never display server bodies or raw exceptions containing key.
                kind = str(exc) if isinstance(exc, (ApiError, ReviewResponseError)) else type(exc).__name__
                self.checkFinished.emit(False, context + "测试失败：" + kind + ("；" + "；".join(warnings) if warnings else ""))
        threading.Thread(target=work, daemon=True).start()

    def _check_finished(self, ok: bool, message: str):
        self._testing = False
        self.test_btn.setEnabled(self._editor_unlocked)
        self.config_note.setText(message)

    def _resume(self):
        entry = self.task_list.currentItem()
        if entry is not None:
            self.owner.resume_api_task(entry.data(Qt.UserRole), selected=self._checked_comics() or None)

    def _open_logs(self):
        entry = self.task_list.currentItem()
        if entry is not None:
            self.owner.open_task_log_folder(entry.data(Qt.UserRole))

    def _filter_refresh(self,*_args):
        self.refresh(force_filter=True)

    def refresh(self, *_args, force_filter=False):
        perf = getattr(self.owner, '_perf_diag', None)
        started = perf_counter() if perf else 0
        stage = started
        config = self.owner.current_api_config()
        if not self._config_loaded:
            self._load_profile(config)
            self._config_loaded = True
        if config:
            provider = PROVIDERS.get(provider_for(config), "其他兼容接口")
            mode = default_thinking_label(config) if thinking_mode(config)=="default" else THINKING_MODES.get(thinking_mode(config), "接口默认")
            self.saved_config_label.setText(_redact(f"正式送审配置：{config.get('name', '原有配置')}"))
            self.saved_model_label.setText(_redact(f"{provider}｜模型 {config.get('model', '')}｜推理 {mode}"))
        else:
            self.saved_config_label.setText("尚无已保存配置，请点“编辑配置”填写并保存。")
            self.saved_model_label.clear()
        self.saved_model_label.setVisible(bool(config))
        self.supplement_label.setText(f"共用补充依据：启用 {len(enabled_supplements(self.owner.settings_data))} 条｜新API / TXT任务使用保存快照")
        self._update_scope_count()
        if perf:
            stage = perf.mark('ai.refresh.config', stage)
        self.txt_page.refresh()
        self._refresh_directory_labels()
        if perf:
            stage = perf.mark('ai.refresh.txt', stage)
        selected = self.task_list.currentItem()
        selected_id = selected.data(Qt.UserRole) if selected else None
        self._retain_task_id = None if force_filter else selected_id
        old_list_scroll = self.task_list.verticalScrollBar().value()
        self.task_list.blockSignals(True)
        self.task_list.clear()
        all_tasks = sorted(reversed(self.owner.review_tasks()), key=lambda t:(self._pending_task(t),t.get('created_at','')),reverse=True)
        for task in all_tasks:
            if not self._task_visible(task): continue
            rows = list(task.get("items", {}).values())
            success = sum(x.get("state") == "success" for x in rows)
            failure = sum(x.get("state") == "failed" for x in rows)
            waiting = sum(x.get("state") in {"pending", "dispatching", "uncertain"} for x in rows)
            if is_txt(task):
                c = counts(task)
                caption = f"{task['created_at'][:16].replace('T', ' ')} TXT {TASK_LABELS.get(task.get('state'), task.get('state'))} 共 {len(rows)} 本｜成功 {c['success']}｜失败 {c['failed']}｜待结果 {c['pending']}"
            else:
                caption = f"{task['created_at'][:16].replace('T',' ')} API {self._state_label(task.get('state', ''))} 共 {len(rows)} 本｜成功 {success}｜失败 {failure}｜待处理 {waiting}"
            entry = QListWidgetItem(caption + (' ｜当前库' if self.owner.review_task_is_current_library(task) else ' ｜历史库'))
            entry.setData(Qt.UserRole, task["task_id"])
            self.task_list.addItem(entry)
            if task["task_id"] == selected_id:
                self.task_list.setCurrentItem(entry)
        if not self.task_list.currentItem() and self.task_list.count():
            self.task_list.setCurrentRow(0)
        self.task_list.blockSignals(False)
        self.task_list.verticalScrollBar().setValue(old_list_scroll)
        if perf:
            stage = perf.mark('ai.refresh.tasks', stage, tasks=self.task_list.count())
        self._show_task(self.task_list.currentRow())
        self.logs_btn.setEnabled(self.task_list.currentItem() is not None)
        running = self.owner._ai_runner
        selected_task = self.owner.review_task(self.task_list.currentItem().data(Qt.UserRole)) if self.task_list.currentItem() else None
        self.stop_btn.setEnabled(bool(running and running.is_alive() and selected_task and running.task_id==selected_task['task_id']))
        if perf:
            perf.mark('ai.refresh.details', stage)
            perf.mark('ai.refresh.total', started)

    @staticmethod
    def _state_label(state):
        return {'running':'运行中','queued':'排队中','completed':'已结束','paused':'暂停','stopped':'已停止','interrupted':'已中断','save_failed':'保存待确认','waiting':'等待结果','export_failed':'部分待导出'}.get(state,state)

    @staticmethod
    def _pending_task(task):
        if task.get('state') in {'completed','stopped','interrupted'}:
            return False
        return task.get('state') in {'running','queued','paused','waiting','export_failed','save_failed'} or any(r.get('state') in {'pending','dispatching','uncertain','export_pending'} for r in task.get('items',{}).values())

    def _task_visible(self, task):
        wanted = self.task_type.currentData()
        if wanted and wanted != ('TXT' if is_txt(task) else 'API'): return False
        state = self.task_state_filter.currentData()
        rows = list(task.get('items',{}).values())
        if state=='pending' and not self._pending_task(task): return False
        if state=='failed' and not any(r.get('state')=='failed' for r in rows): return False
        if state in {'running','completed'} and task.get('state')!=state: return False
        query = self.task_search.text().strip().casefold()
        surface = ' '.join([task.get('task_id',''),task.get('created_at',''),str(task.get('config',{}).get('model','')),*[r.get('input',{}).get('LOCAL',{}).get('local_name','') for r in rows]])
        return not query or query in surface.casefold()

    def _show_task(self, row: int):
        if row < 0:
            self.task_detail.clear()
            self.process_detail.clear(); self.comic_detail.setRowCount(0)
            self._comic_status = {}; self._detail_task_id = None
            self.txt_retry_btn.hide()
            self.remaining_scope_label.clear()
            for w in (self.stop_btn,self.txt_retry_btn,self.txt_folder_btn,self.directory_import_btn): w.setEnabled(False)
            self.remaining_btn.setEnabled(False)
            self.api_txt_btn.setEnabled(False)
            for w in (self.logs_btn,self.copy_diag_btn): w.setEnabled(False)
            self.log_view_btn.setEnabled(True)
            self._refresh_directory_labels()
            return
        task_id = self.task_list.item(row).data(Qt.UserRole)
        task = self.owner.review_task(task_id)
        if task is None:
            return
        old_scroll = self.task_detail.verticalScrollBar().value() if getattr(self, "_shown_task_id", None) == task_id else 0
        self._shown_task_id = task_id
        self.txt_folder_btn.setEnabled(is_txt(task))
        self.txt_retry_btn.setVisible(is_txt(task) and any(p['state']=='export_pending' for p in task.get('parts', [])))
        self.directory_import_btn.setEnabled(is_txt(task) and not self.txt_page.directory_busy)
        self.directory_import_btn.setToolTip('导入选中任务的RESULT' if is_txt(task) else '请先选择对应的TXT任务；API转TXT会生成独立的TXT任务。')
        self._refresh_directory_labels()
        running = self.owner._ai_runner
        is_running = bool(running and running.is_alive() and running.task_id==task_id)
        self.api_txt_btn.setEnabled(False)
        self.stop_btn.setEnabled(is_running)
        for w in (self.logs_btn,self.log_view_btn,self.copy_diag_btn): w.setEnabled(True)
        self.remaining_btn.setEnabled(False)
        if is_txt(task):
            self._show_txt_task(task, old_scroll)
            return
        config = task.get("config", {})
        lines = [f"任务 {task_id}", f"状态：{self._state_label(task.get('state', ''))}｜共 {len(task.get('items',{}))} 本",
                 f"配置：{config.get('profile_name', '旧记录未提供')}｜模型：{config.get('model', '旧记录未提供')}｜推理：{THINKING_MODES.get(config.get('thinking_mode'), '旧记录未提供')}",
                 f"每组 {config.get('batch_size', '旧记录未提供')} 本｜等待 {config.get('timeout_seconds', '未提供')} 秒｜{'总生成 token 上限' if config.get('output_limit_parameter') == 'max_completion_tokens' else '输出上限'} {config.get('max_tokens', '旧记录未提供')}"]
        rows = list(task.get('items',{}).values())
        lines.append(f"进度：成功 {sum(r.get('state')=='success' for r in rows)}｜失败 {sum(r.get('state')=='failed' for r in rows)}｜待处理 {sum(r.get('state') in {'pending','dispatching','uncertain'} for r in rows)}")
        lines.append(f"并发上限 {config.get('concurrency',2)}｜当前处理 {task.get('in_flight_groups',0)} 组｜重试上限 {config.get('max_retries',2)}｜思考强度 {EFFORTS.get(config.get('effort','default'),'接口默认')}")
        lines.append(f"本任务补充依据快照：{len(task.get('supplements',[]))} 条")
        if task.get("continuation"):
            lines.append("接续任务：" + task["continuation"])
        if isinstance(task.get("elapsed_ms"), (int, float)):
            lines.append(f"任务运行：{task['elapsed_ms'] / 1000:.2f} 秒")
        requests = task.get("requests")
        if isinstance(requests, list):
            lines += usage_lines(requests)
            lines.append(cost_line(task))
            groups = {}
            for request in requests: groups.setdefault(request.get('group'),[]).append(request)
            for group, attempts in sorted(groups.items()):
                last = attempts[-1]
                phase = task.get('group_status',{}).get(str(group),{})
                if phase.get('phase')=='waiting' and task.get('state')=='running':
                    end = f"等待重试（最多 {phase.get('seconds',0):g} 秒），尚未最终失败"
                elif phase.get('phase')=='requesting' and task.get('state')=='running':
                    end = '正在请求（前次错误仅供诊断）'
                elif last.get('error'):
                    end = '最终失败：'+last['error']
                elif last.get('validation_failed_items'):
                    end = f"有效 {last.get('validated_items',0)} / 无效 {last['validation_failed_items']}"
                elif last.get('validated_items'):
                    end = ('重试后成功' if len(attempts)>1 else '成功')
                else:
                    end = last.get('finish_reason') or '返回待核验'
                lines.append(f"第 {group} 组｜{len(attempts)} 次尝试｜{end}")
        else:
            lines.append("用量：旧任务未保存本版摘要字段；可从原任务日志核对。")
        lines.append("")
        if task.get("persistence_warning"):
            lines += [task["persistence_warning"], ""]
        if task.get("diagnostic_warnings"):
            lines += ["日志/原文保存警告：", *task["diagnostic_warnings"], ""]
        self.task_detail.setPlainText(_redact("\n".join(lines)))
        self.task_detail.verticalScrollBar().setValue(old_scroll)
        process_lines = []
        for request in (requests if isinstance(requests,list) else []):
            process_lines.append(f"第 {request.get('group')} 组｜尝试 {request.get('attempt')}｜{request.get('item_count')} 本｜{request.get('attempt_ms',0)/1000:.2f} 秒｜{request.get('error') or request.get('finish_reason') or '未提供'}")
        self._set_details(process_lines, task_id)


    def _show_txt_task(self, task, scroll):
        c = counts(task)
        lines = ["任务 " + task["task_id"], f"TXT｜{TASK_LABELS.get(task['state'], task['state'])}｜共 {len(task['items'])} 本",
                 "｜".join(f"{ROW_LABELS[k]} {v}" for k, v in c.items()),
                 "导出目录：" + task.get('last_export_dir', task['export_dir']),
                 "有效结果会直接应用到程序建议名；人工名字与共享安全规则仍受保护。",
                 "待结果项与失败项可选中后补审；资料变化项需新任务。"]
        exported = sum(p['state'] == 'exported' for p in task['parts'])
        returned = sum(bool(p.get('received_results', p['imports'])) for p in task['parts'])
        cancelled = sum(p['state'] == 'cancelled' for p in task['parts'])
        lines.append(f"文件记录：已导出 {exported}/{len(task['parts']) - cancelled}｜收到RESULT {returned} 份；完成度按漫画结果计算")
        if cancelled:
            lines.append(f"另有 {cancelled} 份未写出的补审已停止重发，记录保留在详情。")
        if task.get('diagnostic_warnings'):
            lines += ["日志保存提示：", *task['diagnostic_warnings']]
        self.txt_retry_btn.setEnabled(any(p['state'] == 'export_pending' for p in task['parts']))
        process_lines = []
        for part in task['parts']:
            valid = sum(task['items'][i]['state'] == 'success' for i in part['ids'])
            failed = sum(task['items'][i]['state'] == 'failed' for i in part['ids'])
            export_label = {'export_pending': '待重试导出', 'exported': '已导出', 'cancelled': '已停止重发'}.get(part['state'], part['state'])
            return_label = '已收到RESULT' if part.get('received_results', part['imports']) else '本文件尚未返回'
            process_lines.append(f"文件 {part['number']}｜{len(part['ids'])} 本｜{export_label}｜{return_label}｜漫画当前有效 {valid}｜失败 {failed}" + ("｜补审" if part['supplement'] else ""))
            if part.get('reason'): process_lines.append("  " + part['reason'])
            if part.get('path'): process_lines.append("  " + part['path'])
        self.task_detail.setPlainText(_redact("\n".join(lines)))
        self.task_detail.verticalScrollBar().setValue(scroll)
        self._set_details(process_lines, task["task_id"])

    def _checked_comics(self):
        return [self.comic_detail.item(n, 0).data(Qt.UserRole) for n in range(self.comic_detail.rowCount())
                if self.comic_detail.item(n, 0).checkState() == Qt.Checked]

    def _set_details(self, process_lines, task_id):
        same = getattr(self, '_detail_task_id', None) == task_id
        checked = set(self._checked_comics()) if same else set()
        bar = self.process_detail.verticalScrollBar(); scroll = bar.value() if same else 0
        self.process_detail.setPlainText(_redact('\n'.join(process_lines)) or '暂无记录。')
        bar.setValue(scroll)
        table = self.comic_detail
        table_scroll = table.verticalScrollBar().value() if same else 0
        selected_rows = {table.item(n,0).data(Qt.UserRole) for n in {idx.row() for idx in table.selectedIndexes()}} if same else set()
        task = self.owner.review_task(task_id)
        try:
            if self.owner._task_by_id(task_id) is not None:
                by_id = {i.local_id:i for i in self.owner.all_items}
                work = self.owner.work_edit.text().strip()
            else:
                context = self.owner.txt_repository().context(task_id)
                by_id, work = context.by_id, context.payload.get('work_directory', '')
        except (OSError, ValueError):
            by_id, work = {}, ''
        self._comic_work_available = accessible(work)
        self._comic_status = {}
        table.blockSignals(True)
        table.setRowCount(len(task.get('items', {})))
        for n, (local_id, row) in enumerate(task.get('items', {}).items()):
            status = remaining_status(row, by_id.get(local_id))
            self._comic_status[local_id] = status
            checkbox = QTableWidgetItem()
            checkbox.setData(Qt.UserRole, local_id)
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            if status in {'ready', 'changed'}:
                checkbox.setFlags(checkbox.flags() | Qt.ItemIsUserCheckable)
                checkbox.setCheckState(Qt.Checked if local_id in checked else Qt.Unchecked)
            else:
                checkbox.setCheckState(Qt.Unchecked)
            title = row.get('input', {}).get('LOCAL', {}).get('local_name', '')
            state_labels = ROW_LABELS if is_txt(task) else {'pending':'待处理','dispatching':'请求中','uncertain':'待确认','failed':'审核失败','success':'审核成功','superseded':'已接替'}
            state = state_labels.get(row.get('state'),row.get('state', ''))
            extra = {'changed':'资料变化，右键新建复核', 'unavailable':'原文件暂不可访问',
                     'superseded':'较新轮次已接替', 'missing':'原漫画记录不可用'}.get(status, '')
            if extra: state += ' · ' + {'changed':'资料变化','unavailable':'暂不可访问','superseded':'已接替','missing':'记录不可用'}[status]
            values = [title, state, row.get('decision', ''), row.get('suggested_name', ''),
                      '｜'.join(str(row.get(k, '')) for k in ('reason','apply') if row.get(k))]
            table.setItem(n, 0, checkbox)
            for col, value in enumerate(values, 1):
                cell = QTableWidgetItem(_redact(str(value))); cell.setToolTip(cell.text() + ('\n'+extra if col==2 and extra else '')); table.setItem(n, col, cell)
            checkbox.setToolTip('勾选后，两个剩余项按钮只处理勾选范围；不勾选时处理全部可复核剩余项。' if status=='ready' else extra)
            if local_id in selected_rows:
                table.item(n,1).setSelected(True)
        table.blockSignals(False)
        table.verticalScrollBar().setValue(table_scroll)
        self._detail_task_id = task_id
        self._remaining_scope_changed()

    def _comic_header_clicked(self, column):
        if column != 0:
            return
        entries = [self.comic_detail.item(n,0) for n in range(self.comic_detail.rowCount())
                   if self._comic_status.get(self.comic_detail.item(n,0).data(Qt.UserRole)) == 'ready']
        check = not entries or not all(e.checkState()==Qt.Checked for e in entries)
        self.comic_detail.blockSignals(True)
        for n in range(self.comic_detail.rowCount()):
            e = self.comic_detail.item(n,0)
            if not check or e in entries: e.setCheckState(Qt.Checked if check else Qt.Unchecked)
        self.comic_detail.blockSignals(False)
        self._remaining_scope_changed()

    def _remaining_scope_changed(self, *_args):
        if hasattr(self, 'comic_detail'):
            self.comic_detail.horizontalHeader().viewport().update()
        if not hasattr(self, 'remaining_scope_label'):
            return
        entry = self.task_list.currentItem()
        task = self.owner.review_task(entry.data(Qt.UserRole)) if entry else None
        if not task:
            return
        statuses = getattr(self, '_comic_status', {})
        checked = self._checked_comics()
        ready = [i for i in (checked or list(statuses)) if statuses.get(i)=='ready']
        sizes = split_sizes(len(ready), self.txt_page.mode.currentData(), self.txt_page.value.value())
        text = (f'已勾选 {len(checked)} 本｜可复核 {len(ready)} 本' if checked else f'全部可复核剩余项：{len(ready)} 本') + f'｜TXT预计 {len(sizes)} 份'
        from PySide6.QtGui import QFontMetrics
        self.remaining_scope_label.setText(QFontMetrics(self.font()).elidedText(text,Qt.ElideRight,max(100,self.remaining_scope_label.width())))
        self.remaining_scope_label.setToolTip(text + '；资料变化项在漫画明细中右键新建复核。')
        runner = self.owner._ai_runner
        active = bool(runner and runner.is_alive() and runner.task_id==task['task_id'])
        available = bool(ready) and getattr(self, '_comic_work_available', False) and not active
        self.api_txt_btn.setEnabled(available)
        current_session = self.owner._task_by_id(task['task_id']) is not None
        self.remaining_btn.setEnabled(available and current_session)
        if not self.owner.review_task_is_current_library(task):
            for button in (self.remaining_btn, self.api_txt_btn, self.txt_retry_btn, self.directory_import_btn):
                button.setEnabled(False)
        self.remaining_btn.setToolTip('按已保存API配置开始新轮次；继承本任务补充依据。旧TXT结果晚到将忽略。' if current_session else '请先载入原会话，再使用API复核剩余项。')
        self.api_txt_btn.setToolTip('使用TXT页的拆分设置；TXT任务沿用原任务补审，API任务建立关联的TXT任务。')

    def _comic_menu(self, point):
        entry = self.task_list.currentItem()
        task = self.owner.review_task(entry.data(Qt.UserRole)) if entry else None
        if not task or not is_txt(task):
            return
        selected = [i for i in self._checked_comics() if self._comic_status.get(i)=='changed']
        menu = QMenu(self)
        action = menu.addAction('重新复核资料变化项')
        action.setEnabled(bool(selected))
        folder = menu.addAction('打开导出文件夹')
        chosen = menu.exec(self.comic_detail.viewport().mapToGlobal(point))
        if chosen is action:
            self.owner.rereview_txt(task['task_id'], selected, self.txt_page.mode.currentData(), self.txt_page.value.value())
        elif chosen is folder:
            self.owner.open_txt_folder(task['task_id'])

    def _import_selected_directory(self):
        entry = self.task_list.currentItem()
        if entry is not None:
            self.txt_page.import_directory(entry.data(Qt.UserRole))

    def _refresh_directory_labels(self):
        self.archive_controls.refresh()
        from PySide6.QtGui import QFontMetrics
        directory = str(self.owner.settings_data.get('txt_result_directory',''))
        set_address(self.directory_label, directory)
        busy = self.txt_page.directory_busy
        self.directory_change_btn.setEnabled(not busy)
        entry = self.task_list.currentItem()
        task = self.owner.review_task(entry.data(Qt.UserRole)) if entry else None
        target = ('目录导入目标：' + task['task_id']) if task and is_txt(task) else ('选中的是API任务，请选择对应的TXT任务导入RESULT。' if task else '请选择要接收RESULT的TXT任务。')
        self.directory_target_label.setText(QFontMetrics(self.font()).elidedText(target,Qt.ElideMiddle,max(100,self.directory_target_label.width())))
        self.directory_target_label.setToolTip(target)
        report = getattr(self.owner, '_last_txt_report', None)
        self.import_details_btn.setEnabled(True)
        feedback = self.txt_page.directory_status
        if not feedback and report:
            targets = report.get('directory', {}).get('task_id') or '、'.join(report.get('task_ids', [])) or '手动导入（未核对出目标任务）'
            feedback = f"最近导入：{targets}｜审核成功 {report['success']}｜失败 {report['failed']}｜文件错误 {report['file_errors']}"
            from .ai_archive import archive_line
            if archive_line(report): feedback += '\n' + archive_line(report)
        feedback = feedback or '最近导入：暂无'
        feedback = _redact(feedback)
        first, _, extra = feedback.partition('\n')
        self.directory_feedback.setText(QFontMetrics(self.directory_feedback.font()).elidedText(
            first,Qt.ElideRight,max(100,self.directory_feedback.width())))
        self.directory_feedback.setToolTip(first)
        self.directory_extra_feedback.setText(extra)
        self.directory_extra_feedback.setVisible(bool(extra))

    def _show_import_details(self):
        self.txt_page.refresh()
        self.import_anomalies.setPlainText(self.txt_page.anomalies.toPlainText())
        opened = self.import_anomalies.isHidden()
        splitter = self.task_splitter
        if opened:
            self._anomaly_splitter_limits = (splitter.minimumHeight(), splitter.maximumHeight())
            sizes = splitter.sizes()
            splitter.setFixedHeight(splitter.height())
            before = self.height()
            extra = self.import_anomalies.height() + self.task_scroll.widget().layout().spacing()
            self.import_anomalies.show()
            if not self.isMaximized():
                available = self.screen().availableGeometry()
                room = max(0, available.bottom()-self.frameGeometry().bottom())
                self.resize(self.width(), before+min(extra, room))
            self._anomaly_window_growth = self.height()-before
            self.task_scroll.widget().layout().activate()
            splitter.setSizes(sizes)
        else:
            sizes = splitter.sizes()
            self.import_anomalies.hide()
            low, high = self._anomaly_splitter_limits
            splitter.setMinimumHeight(low); splitter.setMaximumHeight(high)
            if not self.isMaximized():
                self.resize(self.width(), self.height()-self._anomaly_window_growth)
            self.task_scroll.widget().layout().activate()
            splitter.setSizes(sizes)

    def _init_comic_column_widths(self):
        self._comic_widths_timer = QTimer(self)
        self._comic_widths_timer.setSingleShot(True)
        self._comic_widths_timer.setInterval(350)
        self._comic_widths_timer.timeout.connect(self._save_comic_column_widths)
        widths = self.owner.settings_data.get('ai_comic_column_widths')
        table = self.comic_detail
        if isinstance(widths, list) and len(widths)==table.columnCount() and all(type(w) is int and 45<=w<=4000 for w in widths):
            for column,width in enumerate(widths): table.setColumnWidth(column,width)
            table._initial_widths = True
        table.horizontalHeader().sectionResized.connect(lambda *_: self._comic_widths_timer.start())

    def _save_comic_column_widths(self):
        table = self.comic_detail
        if not table._initial_widths: return
        widths = [table.columnWidth(i) for i in range(table.columnCount())]
        if self.owner.settings_data.get('ai_comic_column_widths') != widths:
            self.owner._set_setting('ai_comic_column_widths', widths)

    def _align_form_labels(self):
        labels = [*getattr(self, '_config_form_labels', []), self.scope_label, self.profiles_label]
        if labels:
            width = max(self.fontMetrics().horizontalAdvance(label.text()) for label in labels) + 4
            for label in labels: label.setMinimumWidth(width)
        for label,field in getattr(self,'_config_form_fields',[]):
            height = field.sizeHint().height()
            field.setFixedHeight(height)
            field.setMaximumWidth(560)
            label.setFixedHeight(height)
        for label,field in ((self.scope_label,self.scope),(self.profiles_label,self.profiles)):
            label.setAlignment(Qt.AlignLeft|Qt.AlignVCenter)
            label.setFixedHeight(field.sizeHint().height())

    def _sync_review_fonts(self):
        # Scoped color styles can reset inherited fonts when Qt propagates a change.
        # Apply the shared font after propagation; no page uses a separate size.
        font = self.font()
        for widget in self.findChildren(QWidget):
            widget.setFont(font)
        self._style_review_forms()
        self._align_form_labels()
        self._size_supplement_strip()

    def _style_review_forms(self):
        inset = max(10, self.fontMetrics().horizontalAdvance('一'))
        height = max(26, self.fontMetrics().height()+12)
        self.api_scroll.widget().setStyleSheet(
            'QWidget#reviewApiPage, QWidget#reviewApiEditor { background-color: white; }'
            f'QLineEdit, QComboBox {{ padding: 0px; min-height: {height-4}px; }}'
            'QComboBox QLineEdit { padding: 0px; min-height: 0px; border: none; }'
            'QToolButton[reviewFoldHeader="true"] { border: none; background: transparent; padding: 5px 8px; text-align: left; }'
            'QToolButton[reviewFoldHeader="true"]:hover { background: #eef1f4; }'
            'QToolButton[reviewFoldHeader="true"]:pressed { background: #e1e6ec; }')
        # Use the same full header row for both folds; no native half-border.
        for toggle in (self.advanced_toggle, self.price_toggle):
            toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            toggle.setMinimumHeight(height)
        self.txt_page.setStyleSheet(
            f'QSpinBox {{ padding: 0px; min-height: {height-4}px; }}'
            f'QLineEdit, QComboBox {{ padding: 0px; min-height: {height-4}px; }}')
        self.scope.ensurePolished(); self.txt_page.scope.ensurePolished()
        scope_height = max(self.scope.sizeHint().height(), self.txt_page.scope.sizeHint().height())
        self.scope.setFixedHeight(scope_height)
        self.txt_page.scope.setFixedHeight(scope_height)
        self.txt_page.mode.ensurePolished(); self.txt_page.value.ensurePolished()
        split_height = max(height, self.txt_page.mode.sizeHint().height(), self.txt_page.value.sizeHint().height())
        self.txt_page.mode.setFixedHeight(split_height); self.txt_page.value.setFixedHeight(split_height)

    def _size_supplement_strip(self):
        if hasattr(self,'supplement_strip'):
            margins = self.supplement_strip.layout().contentsMargins()
            text_height = self.supplement_label.fontMetrics().lineSpacing()*2
            buttons = self.supplement_strip.findChildren(QPushButton)
            button_height = max((button.sizeHint().height() for button in buttons),default=0)
            self.supplement_strip.setFixedHeight(max(text_height,button_height)+margins.top()+margins.bottom())

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type()==QEvent.FontChange and hasattr(self, 'txt_page'):
            QTimer.singleShot(0, self._sync_review_fonts)
            self._align_form_labels()
        if event.type()==QEvent.FontChange and hasattr(self, 'directory_feedback'):
            self.directory_target_label.setFixedHeight(self.fontMetrics().height()+4)
            self.remaining_scope_label.setFixedHeight(self.fontMetrics().height()+6)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self,'directory_label'):
            self._refresh_directory_labels()
            self.txt_page.refresh_directory()

    def _txt_action(self, action):
        entry = self.task_list.currentItem()
        if entry is None: return
        task_id = entry.data(Qt.UserRole)
        if action == 'retry': self.owner.retry_txt_export(task_id); return
        if action == 'folder': self.owner.open_txt_folder(task_id); return
        if action == 'all': self._comic_header_clicked(0); return
        selected = self._checked_comics()
        if not selected:
            QMessageBox.information(self, "请选择项目", "在漫画明细中勾选需要重新复核的资料变化项。")
            return
        method = self.owner.rereview_txt
        method(task_id, selected, self.txt_page.mode.currentData(), self.txt_page.value.value())
