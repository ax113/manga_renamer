from __future__ import annotations

import json
import os
import re
import ntpath
import pickle
import copy
import uuid
import shlex
import subprocess
import tempfile
import webbrowser
from collections import defaultdict
from concurrent.futures import Future, ProcessPoolExecutor
from datetime import datetime
from dataclasses import asdict
from multiprocessing import get_context
from pathlib import Path
from time import perf_counter, time_ns

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer, QUrl
from PySide6.QtGui import QAction, QCursor, QGuiApplication, QFontMetrics, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QDockWidget,
    QDialog,
    QDialogButtonBox,
    QCheckBox,
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QSizePolicy,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QListWidget,
    QListWidgetItem,
    QWidget,
)

from .ui_common import AlignedComboBox
from .card_model import CardListModel
from .card_view import CardListView
from .app_paths import data_dir, logs_dir, log_roots
from .database import DatabaseInfo, cleanup_snapshots, create_snapshot, load_records, validate_database
from .analyzer import ANALYZER_VERSION, analyze_items
from .ai_dialog import AiDialog
from .ai_txt_controller import TxtController
from .ai_config import active_config, profile_state, saved_state, new_profile_id, thinking_mode, runtime_values
from .ai_supplements import enabled_supplements, validate_supplements, current_input_fingerprint
from .ai_cost import estimate_cost
from .ai_review import (
    AI_FAILED, AI_REVIEWED, AI_STATES, AI_UNREVIEWED,
    business_input, fingerprint, input_item, safe_name_application, apply_review_output,
)
from .ai_transport import AiSignals, ApiRunner, validate_config, runtime_config
from .ai_log import TaskLog, task_log_dir
from .models import (
    CATEGORIES,
    ATTRIBUTE_ARCHIVE,
    ATTRIBUTE_COSPLAY,
    ATTRIBUTE_GALLERY_CONFLICT,
    CATEGORY_REVIEW,
    CATEGORY_LLM_REVIEW,
    CATEGORY_AI_REVIEWED,
    CATEGORY_CONFIRMED,
    CATEGORY_SUGGESTED,
    CATEGORY_UNCHANGED,
    WorkItem,
)
from .pagination import (
    DEFAULT_PAGE_SIZE,
    PAGE_SIZES,
    PageAnchor,
    clamp_page,
    ensure_unique_local_ids,
    group_and_sort,
    ordered_all_export,
    ordered_categories_export,
    page_count,
    page_slice,
    resolve_anchor_page,
    sort_items,
)
from .scanner import match_entries, scan_work_directory
from .run_log import log_message, log_exception, register_secret, _redact
from .version import VERSION
from .operation_preview import OperationPreviewDialog
from .file_controller import FileController
from .path_rules import name_problem, path_key
from .perf_diagnostic import PerfDiagnostic
from .session_store import (
    archive_session,
    create_checkpoint,
    delete_session,
    item_from_dict,
    item_to_dict,
    load_session,
    save_session,
)
from .session_worker import write_frozen_session
from .undo_history import (
    UndoChange,
    UndoHistory,
    UndoRecord,
    apply_attribute_state,
    apply_classification_state,
    snapshot_attribute_state,
    snapshot_classification_state,
)


SEARCH_RESULTS_LABEL = "搜索结果"


class CollapsibleSection(QWidget):
    """控制中心里的紧凑手风琴区域。"""

    def __init__(self, title: str, content: QWidget, expanded: bool = True, parent=None):
        super().__init__(parent)
        self.content = content

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.header = QToolButton()
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setChecked(expanded)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.header.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        from .ui_common import CONTROL_BORDER
        self.header.setStyleSheet(
            "QToolButton { text-align: left; font-weight: 600; padding: 5px 6px; "
            "border: 1px solid palette(mid); border-radius: 4px; }"
            f"QToolButton:enabled {{ border-color: {CONTROL_BORDER}; }}"
            "QToolButton:enabled:hover:!pressed { background-color: #d2d5d9; }"
        )
        self.header.toggled.connect(self._set_expanded)

        layout.addWidget(self.header)
        layout.addWidget(content)
        content.setVisible(expanded)

    def _set_expanded(self, expanded: bool):
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.content.setVisible(expanded)

    def is_expanded(self) -> bool:
        return self.header.isChecked()

    def set_expanded(self, expanded: bool):
        self.header.setChecked(bool(expanded))


class MainWindow(FileController, TxtController, QMainWindow):
    def __init__(self, safe_layout: bool = False):
        super().__init__()
        self.safe_layout = bool(safe_layout)
        self.setWindowTitle(f"漫画批量改名工具 V{VERSION}")
        self.resize(1420, 900)

        self.settings_data = self._load_settings_file()
        for profile in profile_state(self.settings_data)[0]:
            register_secret(profile.get("key", ""))
        self._cleanup_old_snapshots()
        self.snapshot_info: DatabaseInfo | None = None
        self.records = []
        self.all_items: list[WorkItem] = []
        self.category_items: dict[str, list[WorkItem]] = {category: [] for category in CATEGORIES}
        self.models: dict[str, CardListModel] = {}
        self.views: dict[str, CardListView] = {}
        self.search_model: CardListModel | None = None
        self.search_view: CardListView | None = None
        self.search_results: list[WorkItem] = []
        self.search_tab_active = False
        self.category_pages: dict[str, int] = {category: 1 for category in CATEGORIES}
        self.search_page = 1
        try:
            saved_page_size = int(self.settings_data.get("main_page_size", DEFAULT_PAGE_SIZE))
        except (TypeError, ValueError):
            saved_page_size = DEFAULT_PAGE_SIZE
        self.page_size = saved_page_size if saved_page_size in PAGE_SIZES else DEFAULT_PAGE_SIZE
        self._search_query = ""
        self._pre_search_category = CATEGORIES[0]
        self._last_ordinary_category = CATEGORIES[0]
        self._active_scope_key = CATEGORIES[0]
        self._suppress_tab_change = False
        self._batch_busy = False
        self._page_check_state_before = Qt.Unchecked
        self._pager_compact = False
        self._conflict_validation_signature = None
        self._preview_validation_signature = None
        # BATCH-UNDO-01：仅保存当前运行期的状态操作历史，不写入会话。
        # 名称/名称版本不属于这套 Undo；跨重启持久化留待 AI 工作流稳定后再设计。
        self._undo_history = UndoHistory(max_steps=50)
        self.session_id = uuid.uuid4().hex
        self.ai_tasks: list[dict] = []
        self._session_revision = 0
        self._ai_dialog: AiDialog | None = None
        self._ai_runner: ApiRunner | None = None
        self._ai_secret_configs: dict[str, dict] = {}
        self._ai_signals = AiSignals(self)
        self._ai_signals.prepareGroup.connect(self._on_ai_prepare)
        self._ai_signals.groupReady.connect(self._on_ai_group_ready)
        self._ai_signals.done.connect(self._on_ai_done)
        self._ai_signals.diagnostic.connect(self._on_ai_diagnostic)
        self._ai_exit_after_group = False
        self._ai_in_flight = False
        self._ai_closing = False
        self._init_file_controller()
        self.hide_renamed = bool(self.settings_data.get('hide_renamed', False))

        # 会话保存采用短延迟合并写入，避免每次敲一个字符都立刻写文件。
        self.session_save_timer = QTimer(self)
        self.session_save_timer.setSingleShot(True)
        self.session_save_timer.setInterval(450)
        self.session_save_timer.timeout.connect(self._save_current_session)
        self._session_save_executor: ProcessPoolExecutor | None = None
        self._background_session_save: Future | None = None
        self._pending_session_snapshot: tuple[Path, bytes] | None = None
        # Only the original session entries missing on disk; never enter the
        # active list, but stay in the same session until a fresh scan replaces it.
        self._missing_session_items: list[tuple[int, dict]] = []
        self._session_save_poll = QTimer(self)
        self._session_save_poll.setInterval(50)
        self._session_save_poll.timeout.connect(self._poll_background_session_save)
        self._status_message_timer = QTimer(self)
        self._status_message_timer.setSingleShot(True)
        self._status_message_timer.timeout.connect(self._clear_status_message)

        self.search_debounce_timer = QTimer(self)
        self.search_debounce_timer.setSingleShot(True)
        self.search_debounce_timer.setInterval(180)
        self.search_debounce_timer.timeout.connect(self._apply_search_query)

        self._build_ui()
        self._refresh_empty_state()
        self._perf_diag = (
            PerfDiagnostic(self) if "--perf-diag" in QApplication.arguments() else None
        )
        self._update_pager()
        self._restore_settings()
        self._refresh_library_lock_ui()
        self._install_screen_watchers()
        self._update_execute_state()
        # 窗口真正显示后再做一次坐标校正，避免旧显示器坐标把窗口恢复到屏幕外。
        QTimer.singleShot(80, self._apply_startup_window_safety)
        QTimer.singleShot(180, self._offer_restore_and_file_tasks)

    # ───────────────── UI ─────────────────
    def _build_ui(self):
        from .ui_common import ensure_control_style
        from .review_ui import white_page
        ensure_control_style()
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(5, 5, 5, 2)
        root.setSpacing(0)

        # 左侧主区域：尽量只留分类与漫画列表。
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        self.tabs.setStyleSheet(
            "QTabBar::tab { font-weight: normal; padding: 5px 10px; margin: 0px; "
            "border: 1px solid palette(mid); border-bottom: 2px solid transparent; "
            "background: palette(window); } "
            "QTabBar::tab:selected { background: palette(midlight); "
            "border-bottom-color: palette(highlight); }"
        )
        self.main_stack = QStackedWidget()
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.addStretch(1)
        empty_title = QLabel("尚未载入任务")
        empty_title.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(empty_title)
        self.restore_previous_btn = QPushButton("恢复上次任务")
        self.restore_previous_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.restore_previous_btn.clicked.connect(self.restore_previous_task)
        empty_layout.addWidget(self.restore_previous_btn, 0, Qt.AlignHCenter)
        self.restore_previous_info = QLabel()
        self.restore_previous_info.setAlignment(Qt.AlignCenter)
        empty_layout.addWidget(self.restore_previous_info)
        empty_actions = QHBoxLayout()
        empty_actions.addStretch(1)
        self.empty_control_btn = QPushButton("打开控制面板")
        self.empty_control_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.empty_control_btn.clicked.connect(self.show_control_panel)
        self.empty_detail_btn = QPushButton("打开详情面板")
        self.empty_detail_btn.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.empty_detail_btn.clicked.connect(self.show_detail_panel)
        empty_actions.addWidget(self.empty_control_btn)
        empty_actions.addWidget(self.empty_detail_btn)
        empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions)
        empty_layout.addStretch(1)
        self.main_stack.addWidget(self.tabs)
        self.main_stack.addWidget(self.empty_state)
        root.addWidget(self.main_stack, 1)

        for category in CATEGORIES:
            model = CardListModel([])
            model.confirm_confirmed_edit = self._confirm_confirmed_edit
            view = CardListView()
            view.setModel(model)
            view.checkedCountChanged.connect(self.update_selected_count)
            view.currentItemChanged.connect(self.show_item_details)
            view.selectionReplaceRequested.connect(
                lambda ids, keep, scope=category: self._replace_drag_selection(scope, ids, keep)
            )
            view.selectionToggleRequested.connect(
                lambda ids, scope=category: self._toggle_drag_selection(scope, ids)
            )
            model.itemEdited.connect(self._on_item_changed)
            model.manualStateChanged.connect(self._on_manual_state_changed)
            self.models[category] = model
            self.views[category] = view
            view.setContextMenuPolicy(Qt.CustomContextMenu)
            view.customContextMenuRequested.connect(lambda pos, v=view: self._show_card_context_menu(v, pos))
            self.tabs.addTab(view, f"{category} 0")

        # 跨分类搜索结果总览：仅保存对原 WorkItem 的引用，不复制业务数据。
        self.search_model = CardListModel([])
        self.search_model.confirm_confirmed_edit = self._confirm_confirmed_edit
        self.search_view = CardListView()
        self.search_view.is_search_results_view = True
        self.search_view.setModel(self.search_model)
        self.search_view.checkedCountChanged.connect(self.update_selected_count)
        self.search_view.currentItemChanged.connect(self.show_item_details)
        self.search_view.selectionReplaceRequested.connect(
            lambda ids, keep: self._replace_drag_selection(SEARCH_RESULTS_LABEL, ids, keep)
        )
        self.search_view.selectionToggleRequested.connect(
            lambda ids: self._toggle_drag_selection(SEARCH_RESULTS_LABEL, ids)
        )
        self.search_model.itemEdited.connect(self._on_item_changed)
        self.search_model.manualStateChanged.connect(self._on_manual_state_changed)
        self.search_view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.search_view.customContextMenuRequested.connect(
            lambda pos, v=self.search_view: self._show_card_context_menu(v, pos)
        )

        # 分页栏属于主列表，不随右侧控制面板浮动、停靠或隐藏。
        root.addWidget(self._create_pager_widget())
        self.file_notice_btn = QPushButton()
        self.file_notice_btn.hide()
        self.file_notice_btn.clicked.connect(lambda: self.open_file_dialog(str(self.file_notice_btn.property('task_id') or '')))
        root.addWidget(self.file_notice_btn)

        # 主列表快速搜索：停止输入短暂时间后生成独立虚拟分类。
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索名称、标题、tags、URL")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setFixedWidth(190)
        self.search_edit.textChanged.connect(self._on_search_text_changed)

        self.search_filter_btn = QToolButton()
        self.search_filter_btn.setText("筛选")
        self.search_filter_btn.setPopupMode(QToolButton.InstantPopup)
        from PySide6.QtWidgets import QMenu
        search_filter_menu = QMenu(self.search_filter_btn)
        act_cosplay = QAction("分类：Cosplay", self)
        act_cosplay.triggered.connect(lambda: self._insert_search_condition("category:cosplay"))
        search_filter_menu.addAction(act_cosplay)
        act_archive = QAction("属性：画集", self)
        act_archive.triggered.connect(lambda: self._insert_search_condition("archive:true"))
        search_filter_menu.addAction(act_archive)
        search_filter_menu.addSeparator()
        for status in AI_STATES:
            action = QAction(status, self)
            action.triggered.connect(lambda _checked=False, value=status: self._insert_search_condition(value))
            search_filter_menu.addAction(action)
        conflict_action = QAction("属性：画廊关联冲突", self)
        conflict_action.triggered.connect(lambda: self._insert_search_condition("attribute:gallery_conflict"))
        search_filter_menu.addAction(conflict_action)
        search_filter_menu.addSeparator()
        act_clear_filters = QAction("清空搜索条件", self)
        act_clear_filters.triggered.connect(self.search_edit.clear)
        search_filter_menu.addAction(act_clear_filters)
        search_filter_menu.addSeparator()
        self.hide_renamed_action = QAction("隐藏已改名", self)
        self.hide_renamed_action.setCheckable(True)
        self.hide_renamed_action.setChecked(self.hide_renamed)
        self.hide_renamed_action.toggled.connect(self._toggle_hide_renamed)
        search_filter_menu.addAction(self.hide_renamed_action)
        self.search_filter_btn.setMenu(search_filter_menu)
        focus_search = QAction("搜索", self)
        focus_search.setShortcut("Ctrl+F")
        focus_search.triggered.connect(self._focus_search)
        self.addAction(focus_search)
        clear_search = QAction("清空搜索", self)
        clear_search.setShortcut("Esc")
        clear_search.triggered.connect(self._clear_search_if_focused)
        self.addAction(clear_search)

        # 控制面板总入口：普通点击负责“显示/唤回”，箭头菜单负责停靠、浮动、隐藏与布局恢复。
        self.panel_toggle = QToolButton()
        self.panel_toggle.setText("控制面板")
        self.panel_toggle.setToolTip("显示 / 唤回控制面板；右侧箭头可选择停靠、浮动或隐藏")
        self.panel_toggle.clicked.connect(self.show_control_panel)
        self.panel_toggle.setPopupMode(QToolButton.MenuButtonPopup)
        panel_menu = self.panel_toggle.menu()
        if panel_menu is None:
            from PySide6.QtWidgets import QMenu
            panel_menu = QMenu(self.panel_toggle)
            self.panel_toggle.setMenu(panel_menu)

        action_show_panel = QAction("显示 / 唤回控制面板", self)
        action_show_panel.triggered.connect(self.show_control_panel)
        panel_menu.addAction(action_show_panel)

        action_dock_right = QAction("停靠到主窗口右侧", self)
        action_dock_right.triggered.connect(self.dock_control_panel_right)
        panel_menu.addAction(action_dock_right)

        action_float_panel = QAction("浮动为独立面板", self)
        action_float_panel.triggered.connect(self.float_control_panel)
        panel_menu.addAction(action_float_panel)

        action_hide_panel = QAction("隐藏控制面板", self)
        action_hide_panel.triggered.connect(self.hide_control_panel)
        panel_menu.addAction(action_hide_panel)

        panel_menu.addSeparator()
        self.checkpoint_action = QAction("检查点管理", self)
        self.checkpoint_action.triggered.connect(self.open_checkpoint_manager)
        panel_menu.addAction(self.checkpoint_action)
        self.reanalyze_action = QAction("重新分析当前任务", self)
        self.reanalyze_action.triggered.connect(self.reanalyze_current_task)
        self.reanalyze_action.setEnabled(False)
        panel_menu.addAction(self.reanalyze_action)
        panel_menu.addSeparator()
        action_settings = QAction("设置", self)
        action_settings.triggered.connect(self.open_settings_dialog)
        panel_menu.addAction(action_settings)

        panel_menu.addSeparator()
        action_reset_layout = QAction("恢复默认布局", self)
        action_reset_layout.triggered.connect(self.reset_default_layout)
        panel_menu.addAction(action_reset_layout)

        self.detail_toggle = QToolButton()
        self.detail_toggle.setText("详情")
        self.detail_toggle.setToolTip("显示 / 唤回详情面板；右侧箭头可停靠、浮动或隐藏")
        self.detail_toggle.clicked.connect(self.show_detail_panel)
        self.detail_toggle.setPopupMode(QToolButton.MenuButtonPopup)
        from PySide6.QtWidgets import QMenu
        detail_menu = QMenu(self.detail_toggle)
        self.detail_toggle.setMenu(detail_menu)
        act_detail_show = QAction("显示 / 唤回详情面板", self)
        act_detail_show.triggered.connect(self.show_detail_panel)
        detail_menu.addAction(act_detail_show)
        act_detail_dock = QAction("停靠到主窗口右侧", self)
        act_detail_dock.triggered.connect(self.dock_detail_panel_right)
        detail_menu.addAction(act_detail_dock)
        act_detail_float = QAction("浮动为独立面板", self)
        act_detail_float.triggered.connect(self.float_detail_panel)
        detail_menu.addAction(act_detail_float)
        act_detail_hide = QAction("隐藏详情面板", self)
        act_detail_hide.triggered.connect(self.hide_detail_panel)
        detail_menu.addAction(act_detail_hide)

        corner = QWidget()
        corner_lay = QHBoxLayout(corner)
        corner_lay.setContentsMargins(0, 0, 0, 0)
        corner_lay.setSpacing(3)
        self.ai_top_btn = QToolButton()
        self.ai_top_btn.setText("AI复核")
        self.ai_top_btn.clicked.connect(self.open_ai_dialog)
        toolbar_buttons = (
            self.search_filter_btn, self.ai_top_btn,
            self.panel_toggle, self.detail_toggle,
        )
        for button in toolbar_buttons:
            button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        toolbar_controls = (self.search_edit, *toolbar_buttons)
        for control in toolbar_controls:
            corner_lay.addWidget(control, 0, Qt.AlignVCenter)
            control.ensurePolished()
            control.setFont(corner.font())
        # 同一排使用相同字体和原生工具按钮样式；高度取当前字体/主题的
        # 最大建议值，兼容系统缩放，也保留菜单箭头所需的各自宽度。
        toolbar_height = max(control.sizeHint().height() for control in toolbar_controls)
        for control in toolbar_controls:
            control.setFixedHeight(toolbar_height)
        self.tabs.setCornerWidget(corner, Qt.TopRightCorner)

        # 右侧控制中心：本版正式开放“停靠 <-> 浮动”。
        # 只允许停靠在右侧；浮动后可以拖到任意显示器，再拖回主窗口右侧吸附。
        self.control_dock = QDockWidget("控制面板", self)
        self.control_dock.setObjectName("controlDock")
        self.control_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.control_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.control_dock.setMinimumWidth(220)
        self.addDockWidget(Qt.RightDockWidgetArea, self.control_dock)

        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        side_scroll.setFrameShape(QScrollArea.NoFrame)
        self.control_dock.setWidget(side_scroll)

        side_panel = QWidget()
        self.side_panel = side_panel
        side_layout = QVBoxLayout(side_panel)
        side_layout.setContentsMargins(5, 5, 5, 5)
        side_layout.setSpacing(5)
        side_scroll.setWidget(side_panel)
        from .ui_common import panel_background
        for surface in (side_scroll,side_scroll.viewport(),side_panel): panel_background(surface)

        self.sections: dict[str, CollapsibleSection] = {}

        def add_section(key: str, title: str, content: QWidget, default_expanded: bool = True):
            saved_sections = self.settings_data.get("control_sections", {})
            expanded = default_expanded
            if isinstance(saved_sections, dict) and key in saved_sections:
                expanded = bool(saved_sections[key])
            section = CollapsibleSection(title, content, expanded)
            self.sections[key] = section
            # 重要：所有 section 都连续贴在顶部，不给任何 section stretch。
            # 这样折叠后不会再出现“一个在顶、一个在中、一个在底”的巨大空白。
            side_layout.addWidget(section)
            return section

        # 数据源
        source_content = QWidget()
        source_layout = QVBoxLayout(source_content)
        source_layout.setContentsMargins(6, 5, 6, 6)
        source_layout.setSpacing(5)
        source_layout.addWidget(QLabel("源数据库："))
        db_row = QHBoxLayout()
        self.db_edit = QLineEdit()
        self.db_edit.setPlaceholderText("exhentai-manga-manager 的 database.sqlite")
        self.db_choose_btn = QPushButton("选择")
        self.db_choose_btn.clicked.connect(self.choose_database)
        db_row.addWidget(self.db_edit, 1)
        db_row.addWidget(self.db_choose_btn)
        source_layout.addLayout(db_row)

        protect_row = QHBoxLayout()
        self.library_lock_check = QCheckBox("工作库保护（锁定数据源）")
        self.library_lock_check.setChecked(bool(self.settings_data.get("library_lock", True)))
        self.library_lock_check.setToolTip("有当前任务时，锁定后禁止更换数据库；首次无任务时仍可选择数据库。")
        self.library_lock_check.toggled.connect(self._on_library_lock_toggled)
        protect_row.addWidget(self.library_lock_check)
        protect_row.addStretch(1)
        source_layout.addLayout(protect_row)

        source_layout.addWidget(QLabel("本批工作目录："))
        work_row = QHBoxLayout()
        self.work_edit = QLineEdit()
        self.work_edit.setPlaceholderText("本次实际处理的漫画目录")
        work_btn = QPushButton("选择")
        self.work_choose_btn = work_btn
        work_btn.clicked.connect(self.choose_work_dir)
        work_row.addWidget(self.work_edit, 1)
        work_row.addWidget(work_btn)
        source_layout.addLayout(work_row)

        depth_row = QHBoxLayout()
        depth_row.addWidget(QLabel("最大扫描深度："))
        from .review_ui import NoWheelSpinBox
        self.scan_depth_spin = NoWheelSpinBox()
        self.scan_depth_spin.setRange(1, 10)
        self.scan_depth_spin.setValue(2)
        self.scan_depth_spin.setSuffix(" 层")
        self.scan_depth_spin.setToolTip("工作目录的直接子项为第 1 层；当前 F:\\分类\\漫画 的结构使用 2 层即可。")
        depth_row.addWidget(self.scan_depth_spin)
        depth_row.addStretch(1)
        source_layout.addLayout(depth_row)

        self.scan_btn = QPushButton("建立数据库快照并扫描本批")
        self.scan_btn.clicked.connect(self.scan_batch)
        source_layout.addWidget(self.scan_btn)

        self.snapshot_label = QLabel("尚未建立快照")
        self.snapshot_label.setWordWrap(True)
        self.snapshot_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        source_layout.addWidget(self.snapshot_label)
        add_section("source", "数据源与本批工作目录", source_content, True)

        # 本批状态
        status_content = QWidget()
        status_layout = QVBoxLayout(status_content)
        status_layout.setContentsMargins(6, 4, 6, 5)
        status_layout.setSpacing(3)
        self.summary_label = QLabel()
        self.summary_label.hide()
        self.status_values = {}
        status_grid = QGridLayout()
        status_grid.setHorizontalSpacing(8)
        status_grid.setVerticalSpacing(2)
        # 按性质分组，避免“异常/无需修改”“已确认/画集”等不同语义项目被硬拼在同一行。
        # 只调整视觉排列，不改变任何统计项目或统计口径。
        status_groups = [
            [("数据库", "本批"), ("匹配", "未匹配"), ("异常", None)],
            [("无需修改", "建议修改"), (CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED), ("人工复核", "已确认")],
            [("画集", "Cosplay")],
            [("人工修改", "人工确认"), (AI_UNREVIEWED, AI_REVIEWED), (AI_FAILED, None)],
        ]
        row = 0
        for group_index, rows in enumerate(status_groups):
            if group_index:
                status_grid.setRowMinimumHeight(row, 5)
                row += 1
            for names in rows:
                for col, name in enumerate(names):
                    if name is None:
                        continue
                    label = QLabel(f"{name}：")
                    value = QLabel("-")
                    value.setTextInteractionFlags(Qt.TextSelectableByMouse)
                    status_grid.addWidget(label, row, 2 * col)
                    status_grid.addWidget(value, row, 2 * col + 1)
                    self.status_values[name] = value
                row += 1
        status_grid.setColumnStretch(1, 1)
        status_grid.setColumnStretch(3, 1)
        self.selected_label = QLabel("已勾选：0")
        self.session_label = QLabel("会话：尚未开始")
        self.session_label.setWordWrap(True)
        self.status_message_label = QLabel()
        self.status_message_label.setWordWrap(True)
        self.status_message_label.hide()
        status_layout.addLayout(status_grid)
        status_layout.addWidget(self.selected_label)
        status_layout.addWidget(self.session_label)
        status_layout.addWidget(self.status_message_label)
        add_section("status", "本批状态", status_content, True)

        # 批量选择 / 分类
        batch_content = QWidget()
        self.batch_content = batch_content
        batch_layout = QVBoxLayout(batch_content)
        batch_layout.setContentsMargins(6, 4, 6, 6)
        batch_layout.setSpacing(5)
        select_row = QHBoxLayout()
        self.select_all_btn = QPushButton("全选")
        self.select_all_btn.clicked.connect(self.select_all_current)
        self.invert_btn = QPushButton("反选")
        self.invert_btn.clicked.connect(self.invert_current)
        self.clear_selection_btn = QPushButton("清空")
        self.clear_selection_btn.clicked.connect(self.clear_current_selection)
        select_row.addWidget(self.select_all_btn)
        select_row.addWidget(self.invert_btn)
        select_row.addWidget(self.clear_selection_btn)
        batch_layout.addLayout(select_row)

        undo_row = QHBoxLayout()
        self.undo_btn = QPushButton("撤销上一步")
        self.undo_btn.clicked.connect(self.undo_last_state_action)
        self.redo_btn = QPushButton("重做")
        self.redo_btn.clicked.connect(self.redo_last_state_action)
        undo_row.addWidget(self.undo_btn)
        undo_row.addWidget(self.redo_btn)
        batch_layout.addLayout(undo_row)
        self._update_undo_buttons()

        self.batch_mark_grid = QGridLayout()
        self.batch_mark_grid.setColumnStretch(0, 1)
        self.batch_mark_grid.setColumnStretch(1, 1)
        for index, category in enumerate((CATEGORY_UNCHANGED, CATEGORY_SUGGESTED,
                                         CATEGORY_LLM_REVIEW, CATEGORY_REVIEW, CATEGORY_CONFIRMED)):
            button = QPushButton(f"标记为{category}")
            button.clicked.connect(
                lambda _checked=False, target=category: self.move_selected_to(target, manual=True))
            self.batch_mark_grid.addWidget(button, index // 2, index % 2)
        to_archive_btn = QPushButton("切换画集属性")
        to_archive_btn.clicked.connect(lambda: self.toggle_attribute_selected(ATTRIBUTE_ARCHIVE))
        self.batch_mark_grid.addWidget(to_archive_btn, 2, 1)
        batch_layout.addLayout(self.batch_mark_grid)
        clear_mark_btn = QPushButton("解除标记")
        clear_mark_btn.clicked.connect(self.clear_selected_marks)
        batch_layout.addWidget(clear_mark_btn)
        export_report_btn = QPushButton("导出分析报告")
        export_report_btn.setToolTip("可导出已勾选漫画、指定分类或全部漫画的完整分析文本")
        export_report_btn.clicked.connect(self.export_analysis_report)
        batch_layout.addWidget(export_report_btn)
        self.ai_selected_btn = QPushButton("AI复核选中项")
        self.ai_selected_btn.clicked.connect(self.start_api_checked)
        batch_layout.addWidget(self.ai_selected_btn)
        add_section("batch", "批量选择与处理建议", batch_content, True)

        # 详情面板：从控制面板中独立出来，可上下拉伸、停靠、浮动到其他显示器。
        details_content = QWidget()
        panel_background(details_content)
        details_layout = QVBoxLayout(details_content)
        details_layout.setContentsMargins(6, 5, 6, 6)
        details_layout.setSpacing(6)
        detail_actions = QHBoxLayout()
        self.open_gallery_btn = QPushButton("打开画廊")
        self.open_gallery_btn.setEnabled(False)
        self.open_gallery_btn.clicked.connect(self.open_current_gallery)
        self.open_local_btn = QPushButton("打开本地位置")
        self.open_local_btn.setToolTip("打开漫画所在的上一级目录，并在资源管理器中选中该漫画")
        self.open_local_btn.setEnabled(False)
        self.open_local_btn.clicked.connect(self.open_current_local)
        self.open_manga_btn = QPushButton("打开漫画")
        self.open_manga_btn.setToolTip("直接进入漫画文件夹；压缩包则交给系统默认程序打开")
        self.open_manga_btn.setEnabled(False)
        self.open_manga_btn.clicked.connect(self.open_current_manga)
        self.copy_url_btn = QPushButton("复制 URL")
        self.copy_url_btn.setEnabled(False)
        self.copy_url_btn.clicked.connect(self.copy_current_url)
        detail_actions.addWidget(self.open_gallery_btn)
        detail_actions.addWidget(self.open_local_btn)
        detail_actions.addWidget(self.open_manga_btn)
        detail_actions.addWidget(self.copy_url_btn)
        detail_actions.addStretch(1)
        details_layout.addLayout(detail_actions)

        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setMinimumSize(280, 220)
        self.details.setPlaceholderText(
            "点击左侧漫画卡片查看 title / title_jpn / URL / tags 与命名分析。"
        )
        details_layout.addWidget(self.details, 1)

        self.name_version_btn = QPushButton("尚无手动版本")
        self.name_version_btn.setEnabled(False)
        self.name_version_btn.setToolTip("在程序建议与保留的手动版本之间来回切换")
        self.name_version_btn.clicked.connect(self.toggle_name_version)
        details_layout.addWidget(self.name_version_btn)

        self.confirm_name_btn = QPushButton("标记为已确认")
        self.confirm_name_btn.setEnabled(False)
        self.confirm_name_btn.clicked.connect(self.toggle_current_confirmation)
        details_layout.addWidget(self.confirm_name_btn)

        self.detail_dock = QDockWidget("详情面板", self)
        self.detail_dock.setObjectName("detailDock")
        self.detail_dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea)
        self.detail_dock.setFeatures(
            QDockWidget.DockWidgetFeature.DockWidgetClosable
            | QDockWidget.DockWidgetFeature.DockWidgetMovable
            | QDockWidget.DockWidgetFeature.DockWidgetFloatable
        )
        self.detail_dock.setWidget(details_content)
        self.addDockWidget(Qt.RightDockWidgetArea, self.detail_dock)
        # 同在右侧时，上面是控制面板、下面是详情；中间分隔条可上下拖动。
        self.splitDockWidget(self.control_dock, self.detail_dock, Qt.Vertical)

        # 执行方式
        destination_content = QWidget()
        self.destination_content = destination_content
        destination_layout = QVBoxLayout(destination_content)
        destination_layout.setContentsMargins(6, 4, 6, 6)
        destination_layout.setSpacing(5)
        self.in_place_radio = QRadioButton("原地处理")
        self.move_radio = QRadioButton("修改后移动到其他目录")
        self.in_place_radio.setChecked(True)
        self.move_radio.setEnabled(False)
        self.move_radio.setText("改名后移动到其他目录")
        self.move_radio.setToolTip("移动功能以后单独开放；本版只支持原地改名。")
        self.in_place_radio.hide()
        self.move_radio.hide()
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("目标目录")
        self.dest_edit.hide()
        self.dest_edit.textChanged.connect(self._invalidate_execution_validation)

        self.conflict_btn = QPushButton("② 测试重名")
        self.conflict_btn.clicked.connect(self.test_name_conflicts)
        self.preview_btn = QPushButton("③ 生成模拟操作清单")
        self.preview_btn.clicked.connect(self.preview_operations)
        self.preview_btn.setEnabled(False)
        self.execute_btn = QPushButton(f"④ 执行选中项目（V{VERSION} 禁用）")
        self.execute_btn.setEnabled(False)
        # Legacy preview methods stay internal; the only visible entry is file handling.
        self.conflict_btn.hide()
        self.preview_btn.hide()
        self.execute_btn.hide()
        self.file_process_btn = QPushButton("改名 / 移动")
        self.file_process_btn.clicked.connect(lambda: self.open_file_dialog())
        destination_layout.addWidget(self.file_process_btn)
        add_section("destination", "漫画文件操作", destination_content, False)

        # 只有最后一个 stretch：折叠标题永远紧挨在顶部，剩余空白全部落到底部。
        side_layout.addStretch(1)

        self.control_dock.visibilityChanged.connect(self._on_control_dock_visibility_changed)
        self.control_dock.topLevelChanged.connect(self._on_control_dock_top_level_changed)
        self.control_dock.dockLocationChanged.connect(self._on_control_dock_location_changed)
        self.detail_dock.visibilityChanged.connect(self._on_detail_dock_visibility_changed)
        self.detail_dock.topLevelChanged.connect(self._on_detail_dock_top_level_changed)
        self.tabs.currentChanged.connect(self.on_tab_changed)
        self._show_status_message(
            f"V{VERSION}：支持原地改名与同盘移动，请在预览中核对名称和路径。", 6000
        )

    def _show_status_message(self, message: str, timeout: int = 5000):
        self._status_message_timer.stop()
        self.status_message_label.setText(f"状态：{message}" if message else "")
        self.status_message_label.setVisible(bool(message))
        if message and timeout > 0:
            self._status_message_timer.start(timeout)

    def _clear_status_message(self):
        self.status_message_label.clear()
        self.status_message_label.hide()

    def _create_pager_widget(self) -> QWidget:
        self.pager_widget = QWidget(self)
        layout = QGridLayout(self.pager_widget)
        layout.setContentsMargins(7, 4, 7, 4)
        layout.setHorizontalSpacing(7)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(2, 1)

        self.pager_selection_widget = QWidget(self.pager_widget)
        selection_layout = QHBoxLayout(self.pager_selection_widget)
        selection_layout.setContentsMargins(0, 0, 0, 0)
        selection_layout.setSpacing(7)

        self.page_select_check = QCheckBox("本页")
        self.page_select_check.setTristate(True)
        self.page_select_check.pressed.connect(
            lambda: setattr(self, "_page_check_state_before", self.page_select_check.checkState())
        )
        self.page_select_check.clicked.connect(self._toggle_current_page_selection)
        self.page_selected_label = QLabel("本页已选 0/0")
        self.scope_selected_label = QLabel("当前分类共选 0")
        selection_layout.addWidget(self.page_select_check)
        selection_layout.addWidget(self.page_selected_label)
        selection_layout.addWidget(self.scope_selected_label)
        layout.addWidget(self.pager_selection_widget, 0, 0, Qt.AlignLeft)

        self.pager_size_widget = QWidget(self.pager_widget)
        size_layout = QHBoxLayout(self.pager_size_widget)
        size_layout.setContentsMargins(0, 0, 0, 0)
        size_layout.setSpacing(7)
        size_layout.addWidget(QLabel("每页"))
        self.page_size_combo = AlignedComboBox()
        for value in PAGE_SIZES:
            self.page_size_combo.addItem(str(value), value)
        self.page_size_combo.setCurrentIndex(PAGE_SIZES.index(self.page_size))
        self.page_size_combo.currentIndexChanged.connect(self._on_page_size_changed)
        size_layout.addWidget(self.page_size_combo)
        layout.addWidget(self.pager_size_widget, 0, 2, Qt.AlignRight)

        self.pager_navigation_widget = QWidget(self.pager_widget)
        navigation_layout = QHBoxLayout(self.pager_navigation_widget)
        navigation_layout.setContentsMargins(0, 0, 0, 0)
        navigation_layout.setSpacing(7)
        self.prev_page_btn = QPushButton("上一页")
        self.page_number_edit = QLineEdit("1")
        self.page_number_edit.setAlignment(Qt.AlignCenter)
        self.page_number_edit.setFixedWidth(48)
        self.page_total_label = QLabel("/ 1 页")
        self.next_page_btn = QPushButton("下一页")
        self.prev_page_btn.clicked.connect(lambda: self._navigate_to_page(self._current_page_number() - 1))
        self.page_number_edit.editingFinished.connect(self._page_number_edit_finished)
        self.next_page_btn.clicked.connect(lambda: self._navigate_to_page(self._current_page_number() + 1))
        navigation_layout.addWidget(self.prev_page_btn)
        navigation_layout.addWidget(QLabel("第"))
        navigation_layout.addWidget(self.page_number_edit)
        navigation_layout.addWidget(self.page_total_label)
        navigation_layout.addWidget(self.next_page_btn)
        layout.addWidget(self.pager_navigation_widget, 0, 1, Qt.AlignHCenter)
        self.pager_widget.installEventFilter(self)

        self.setTabOrder(self.page_select_check, self.prev_page_btn)
        self.setTabOrder(self.prev_page_btn, self.page_number_edit)
        self.setTabOrder(self.page_number_edit, self.next_page_btn)
        self.setTabOrder(self.next_page_btn, self.page_size_combo)
        return self.pager_widget

    def eventFilter(self, watched, event):
        if watched is getattr(self, "pager_widget", None) and event.type() == QEvent.Resize:
            self._arrange_pager(event.size().width())
        return super().eventFilter(watched, event)

    def _arrange_pager(self, width: int):
        if not hasattr(self, "pager_navigation_widget"):
            return
        side = max(self.pager_selection_widget.sizeHint().width(), self.pager_size_widget.sizeHint().width())
        compact = width < self.pager_navigation_widget.sizeHint().width() + 2 * side + 28
        if compact == self._pager_compact:
            return
        self._pager_compact = compact
        layout = self.pager_widget.layout()
        for widget in (self.pager_selection_widget, self.pager_size_widget, self.pager_navigation_widget):
            layout.removeWidget(widget)
        layout.setColumnMinimumWidth(0, 0 if compact else side)
        layout.setColumnMinimumWidth(2, 0 if compact else side)
        if compact:
            layout.addWidget(self.pager_navigation_widget, 0, 0, 1, 3, Qt.AlignHCenter)
            layout.addWidget(self.pager_selection_widget, 1, 0, 1, 2, Qt.AlignLeft)
            layout.addWidget(self.pager_size_widget, 1, 2, Qt.AlignRight)
        else:
            layout.addWidget(self.pager_selection_widget, 0, 0, Qt.AlignLeft)
            layout.addWidget(self.pager_navigation_widget, 0, 1, Qt.AlignHCenter)
            layout.addWidget(self.pager_size_widget, 0, 2, Qt.AlignRight)

    # ───────────────── 配置 ─────────────────
    def settings_path(self) -> Path:
        return Path(self.tool_dir()) / "settings.json"

    def _load_settings_file(self) -> dict:
        try:
            path = self.settings_path()
            if not path.exists():
                return {}
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _save_settings_file(self):
        try:
            path = self.settings_path()
            temp = path.with_suffix(".json.tmp")
            temp.write_text(json.dumps(self.settings_data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(path)
        except Exception:
            pass

    def _set_setting(self, key: str, value):
        self.settings_data[key] = value
        self._save_settings_file()

    def _analysis_options(self) -> dict:
        return {
            "remove_event_prefix": bool(self.settings_data.get("remove_event_prefix", True)),
        }

    # ───────────────── 主列表分页 / 选择状态 ─────────────────
    def _apply_drag_mode_to_views(self):
        current_page_only = str(
            self.settings_data.get("drag_selection_mode", "clear_other_pages")
        ) == "current_page_only"
        for view in list(getattr(self, "views", {}).values()):
            view.set_drag_resets_current_page_only(current_page_only)
        if getattr(self, "search_view", None) is not None:
            self.search_view.set_drag_resets_current_page_only(current_page_only)

    def _scope_items(self, scope: str | None = None) -> list[WorkItem]:
        key = scope or self.current_category()
        if key == SEARCH_RESULTS_LABEL:
            items = self.search_results
        else:
            items = self.category_items.get(key, [])
        return [i for i in items if '已改名' not in str(i.file_status).split(' · ')] if self.hide_renamed else items

    def _model_for_scope(self, scope: str) -> CardListModel:
        if scope == SEARCH_RESULTS_LABEL and self.search_model is not None:
            return self.search_model
        return self.models[scope]

    def _view_for_scope(self, scope: str) -> CardListView:
        if scope == SEARCH_RESULTS_LABEL and self.search_view is not None:
            return self.search_view
        return self.views[scope]

    def _page_number_for_scope(self, scope: str) -> int:
        return self.search_page if scope == SEARCH_RESULTS_LABEL else self.category_pages.get(scope, 1)

    def _set_page_number_for_scope(self, scope: str, page: int):
        valid = clamp_page(page, len(self._scope_items(scope)), self.page_size)
        if scope == SEARCH_RESULTS_LABEL:
            self.search_page = valid
        else:
            self.category_pages[scope] = valid

    def _current_page_number(self) -> int:
        return self._page_number_for_scope(self.current_category())

    def _current_page_count(self) -> int:
        return page_count(len(self._scope_items()), self.page_size)

    def _capture_page_anchor(self) -> PageAnchor:
        scope = self.current_category()
        model = self._model_for_scope(scope)
        return PageAnchor(
            scope=scope,
            page=self._page_number_for_scope(scope),
            local_ids=tuple(item.local_id for item in model.items),
        )

    def _commit_active_edit(self):
        try:
            self.current_view().commit_active_edit()
        except Exception:
            pass

    def _commit_all_edits(self):
        for view in list(self.views.values()) + ([self.search_view] if self.search_view else []):
            try:
                view.commit_active_edit()
            except Exception:
                pass

    def _clear_current_context(self, *, all_views: bool = False):
        targets = (
            list(self.views.values()) + ([self.search_view] if self.search_view else [])
            if all_views else [self.current_view()]
        )
        for view in targets:
            try:
                view.clear_current_item()
                view.reset_range_anchor()
            except Exception:
                pass
        self.details.clear()
        self._update_name_version_button(None)
        self._update_confirmation_button(None)
        self._update_detail_action_buttons(None)

    def _restore_current_context(self, scope: str, local_id: str) -> bool:
        """Restore the same comic only if it still belongs to the active view."""
        if self.current_category() != scope:
            return False
        items = self._scope_items(scope)
        position = next((i for i, item in enumerate(items) if item.local_id == local_id), None)
        if position is None:
            return False
        target_page = position // self.page_size + 1
        if self._page_number_for_scope(scope) != target_page:
            self._set_page_number_for_scope(scope, target_page)
            self._refresh_scope_page(scope)
        model = self._model_for_scope(scope)
        row = next((i for i, item in enumerate(model.items) if item.local_id == local_id), None)
        if row is None:
            return False
        item = model.items[row]
        self._view_for_scope(scope)._set_current_item(model.index(row, 0))
        self.show_item_details(item)
        self._update_pager()
        return True

    def _restore_current_on_visible_page(self, scope: str, local_id: str) -> bool:
        """Background AI refresh must never jump to follow a moved comic."""
        if self.current_category() != scope:
            return False
        model = self._model_for_scope(scope)
        row = next((i for i, item in enumerate(model.items) if item.local_id == local_id), None)
        if row is None:
            return False
        self._view_for_scope(scope)._set_current_item(model.index(row, 0))
        self.show_item_details(model.items[row])
        return True

    def _refresh_scope_page(self, scope: str, *, scroll_top: bool = False):
        perf = getattr(self, "_perf_diag", None)
        started = perf_counter() if perf else 0
        items = self._scope_items(scope)
        page = clamp_page(self._page_number_for_scope(scope), len(items), self.page_size)
        self._set_page_number_for_scope(scope, page)
        model = self._model_for_scope(scope)
        model.set_items(page_slice(items, page, self.page_size))
        if scroll_top:
            self._view_for_scope(scope).scrollToTop()
        if perf:
            perf.mark("page.rebuild", started, scope=scope, scope_size=len(items), page_size=len(model.items))

    def _refresh_all_page_models(self):
        for category in CATEGORIES:
            self._refresh_scope_page(category)
        if self.search_model is not None:
            self._refresh_scope_page(SEARCH_RESULTS_LABEL)

    def _update_pager(self):
        if not hasattr(self, "page_number_edit"):
            return
        perf = getattr(self, "_perf_diag", None)
        started = perf_counter() if perf else 0
        scope = self.current_category()
        full_items = self._scope_items(scope)
        page_model = self._model_for_scope(scope)
        current_page = clamp_page(self._page_number_for_scope(scope), len(full_items), self.page_size)
        total_pages = page_count(len(full_items), self.page_size)
        self._set_page_number_for_scope(scope, current_page)

        self.page_number_edit.setText(str(current_page))
        self.page_total_label.setText(f"/ {total_pages} 页")
        self.prev_page_btn.setEnabled(current_page > 1 and not self._batch_busy)
        self.next_page_btn.setEnabled(current_page < total_pages and not self._batch_busy)

        page_total = len(page_model.items)
        page_checked = sum(1 for item in page_model.items if item.checked)
        scope_checked = sum(1 for item in full_items if item.checked)
        self.page_selected_label.setText(f"本页已选 {page_checked}/{page_total}")
        prefix = "搜索结果共选" if scope == SEARCH_RESULTS_LABEL else "当前分类共选"
        self.scope_selected_label.setText(f"{prefix} {scope_checked}")
        # Equal side columns keep the navigation's center at the exact center
        # of the main list, even as the selection counts gain digits.
        side_width = max(
            self.pager_selection_widget.sizeHint().width(),
            self.pager_size_widget.sizeHint().width(),
        )
        pager_layout = self.pager_widget.layout()
        pager_layout.setColumnMinimumWidth(0, 0 if self._pager_compact else side_width)
        pager_layout.setColumnMinimumWidth(2, 0 if self._pager_compact else side_width)
        self._arrange_pager(self.pager_widget.width())
        self.page_select_check.blockSignals(True)
        if page_total == 0 or page_checked == 0:
            state = Qt.Unchecked
        elif page_checked == page_total:
            state = Qt.Checked
        else:
            state = Qt.PartiallyChecked
        self.page_select_check.setCheckState(state)
        self.page_select_check.setEnabled(page_total > 0 and not self._batch_busy)
        self.page_select_check.blockSignals(False)
        if perf:
            perf.mark("selection.pager", started, scope=scope, scope_size=len(full_items))

    def _toggle_current_page_selection(self, *_args):
        if self._batch_busy:
            return
        model = self.current_model()
        # Qt 的三态循环可能先到 PartiallyChecked；结果为 Unchecked 仅对应“原来全选后再次点击”。
        clear_page = self.page_select_check.checkState() == Qt.Unchecked
        for item in model.items:
            item.checked = not clear_page
        model.refresh_checks()
        self.update_selected_count()

    def _navigate_to_page(self, requested_page: int):
        if self._batch_busy:
            return
        scope = self.current_category()
        valid = clamp_page(requested_page, len(self._scope_items(scope)), self.page_size)
        old = self._page_number_for_scope(scope)
        self.page_number_edit.setText(str(valid))
        if valid == old:
            self._update_pager()
            return
        self._commit_active_edit()
        self._set_page_number_for_scope(scope, valid)
        self._refresh_scope_page(scope, scroll_top=True)
        self._clear_current_context()
        self._update_pager()
        self._schedule_session_save()

    def _page_number_edit_finished(self):
        text = self.page_number_edit.text().strip()
        if not text.isdigit():
            self.page_number_edit.setText(str(self._current_page_number()))
            return
        self._navigate_to_page(int(text))

    def _on_page_size_changed(self, *_args):
        value = self.page_size_combo.currentData()
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = DEFAULT_PAGE_SIZE
        if value not in PAGE_SIZES or value == self.page_size:
            self._update_pager()
            return
        self._commit_active_edit()
        self.page_size = value
        self.settings_data["main_page_size"] = value
        self._save_settings_file()
        self.category_pages = {category: 1 for category in CATEGORIES}
        self.search_page = 1
        self._refresh_all_page_models()
        self._clear_current_context(all_views=True)
        self._update_pager()
        self._schedule_session_save()

    def _replace_drag_selection(self, scope: str, local_ids, current_page_only: bool):
        if self._batch_busy:
            return
        target_ids = {str(value) for value in (local_ids or set())}
        model = self._model_for_scope(scope)
        clear_items = model.items if current_page_only else self._scope_items(scope)
        for item in clear_items:
            item.checked = False
        by_id = {item.local_id: item for item in model.items}
        for local_id in target_ids:
            if local_id in by_id:
                by_id[local_id].checked = True
        model.refresh_checks()
        self.update_selected_count()

    def _toggle_drag_selection(self, scope: str, local_ids):
        if self._batch_busy:
            return
        target_ids = {str(value) for value in (local_ids or set())}
        by_id = {item.local_id: item for item in self._scope_items(scope)}
        for local_id in target_ids:
            item = by_id.get(local_id)
            if item is not None:
                item.checked = not item.checked
        self._model_for_scope(scope).refresh_checks()
        self.update_selected_count()

    def _find_firefox(self) -> str:
        candidates = [
            os.path.expandvars(r"%ProgramFiles%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe"),
            os.path.expandvars(r"%LOCALAPPDATA%\Mozilla Firefox\firefox.exe"),
        ]
        for path in candidates:
            if path and os.path.isfile(path):
                return path
        return ""

    def open_settings_dialog(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("漫画批量改名工具设置")
        dialog.resize(560, 340)
        layout = QVBoxLayout(dialog)

        naming_box = QWidget(dialog)
        naming_layout = QVBoxLayout(naming_box)
        naming_layout.setContentsMargins(0, 0, 0, 0)
        remove_event = QCheckBox("生成建议名称时去除开头活动/展会标记（如 (C107)）")
        remove_event.setChecked(bool(self.settings_data.get("remove_event_prefix", True)))
        naming_layout.addWidget(remove_event)
        layout.addWidget(naming_box)

        drag_box = QWidget(dialog)
        drag_layout = QVBoxLayout(drag_box)
        drag_layout.setContentsMargins(0, 6, 0, 6)
        drag_layout.addWidget(QLabel("普通拖框选择方式："))
        drag_group = QButtonGroup(dialog)
        drag_clear_all = QRadioButton("普通拖框会清除其他页选择（默认）")
        drag_current_page = QRadioButton("只重置本页选择、保留其他页选择")
        drag_group.addButton(drag_clear_all)
        drag_group.addButton(drag_current_page)
        drag_mode = str(self.settings_data.get("drag_selection_mode", "clear_other_pages"))
        drag_current_page.setChecked(drag_mode == "current_page_only")
        drag_clear_all.setChecked(drag_mode != "current_page_only")
        drag_layout.addWidget(drag_clear_all)
        drag_second_row = QHBoxLayout()
        drag_second_row.setContentsMargins(0, 0, 0, 0)
        drag_second_row.addWidget(drag_current_page)
        drag_help = QToolButton()
        drag_help.setText("?")
        drag_help.setAutoRaise(True)
        drag_help.setToolTip("普通拖框只替换当前页的勾选；其他页已经勾选的漫画保持不变。Ctrl 拖框始终追加/切换。")
        drag_second_row.addWidget(drag_help)
        drag_second_row.addStretch(1)
        drag_layout.addLayout(drag_second_row)
        layout.addWidget(drag_box)

        form = QFormLayout()
        browser_combo = AlignedComboBox()
        browser_combo.addItems(["系统默认浏览器", "Firefox"])
        mode = str(self.settings_data.get("gallery_browser", "default"))
        browser_combo.setCurrentIndex(1 if mode == "firefox" else 0)
        form.addRow("打开画廊：", browser_combo)

        firefox_row = QWidget()
        firefox_lay = QHBoxLayout(firefox_row)
        firefox_lay.setContentsMargins(0, 0, 0, 0)
        firefox_edit = QLineEdit(str(self.settings_data.get("firefox_path", "")))
        if not firefox_edit.text().strip():
            firefox_edit.setText(self._find_firefox())
        browse_btn = QPushButton("浏览")
        def choose_firefox():
            start = firefox_edit.text().strip() or str(Path.home())
            path, _ = QFileDialog.getOpenFileName(dialog, "选择 Firefox", start, "Firefox (firefox.exe);;可执行文件 (*.exe);;所有文件 (*)")
            if path:
                firefox_edit.setText(path)
        browse_btn.clicked.connect(choose_firefox)
        firefox_lay.addWidget(firefox_edit, 1)
        firefox_lay.addWidget(browse_btn)
        form.addRow("Firefox 路径：", firefox_row)
        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        if dialog.exec() == QDialog.Accepted:
            old_remove = bool(self.settings_data.get("remove_event_prefix", True))
            self.settings_data["remove_event_prefix"] = remove_event.isChecked()
            self.settings_data["drag_selection_mode"] = (
                "current_page_only" if drag_current_page.isChecked() else "clear_other_pages"
            )
            self.settings_data["gallery_browser"] = "firefox" if browser_combo.currentIndex() == 1 else "default"
            self.settings_data["firefox_path"] = firefox_edit.text().strip()
            self._save_settings_file()
            self._apply_drag_mode_to_views()
            if self.all_items and old_remove != remove_event.isChecked():
                self._reanalyze_and_refresh(status_message=None)
                self.show_scan_summary()
                self._save_current_session()
                self._show_status_message("设置已保存，并已按新的活动名选项重新分析当前任务。", 5000)
            else:
                self._show_status_message("设置已保存。", 3000)

    def _restore_settings(self):
        self.db_edit.setText(str(self.settings_data.get("source_database", "")))
        self.work_edit.setText(str(self.settings_data.get("work_directory", "")))
        try:
            self.scan_depth_spin.setValue(int(self.settings_data.get("scan_max_depth", 2)))
        except Exception:
            self.scan_depth_spin.setValue(2)
        self.dest_edit.setText(str(self.settings_data.get("destination_directory", "")))
        self._apply_drag_mode_to_views()

        width = self.settings_data.get("control_dock_width")
        if width is None:
            old_sizes = self.settings_data.get("main_splitter_sizes")
            if isinstance(old_sizes, list) and len(old_sizes) == 2:
                width = old_sizes[1]
        try:
            width = int(width) if width is not None else 350
        except Exception:
            width = 350

        if self.safe_layout:
            # 安全布局启动只忽略界面位置，不清除数据库路径、工作目录、会话等数据。
            self._pending_dock_width = 350
            self._pending_dock_visible = True
            self._pending_dock_floating = False
            self._pending_dock_geometry = None
            self._pending_detail_visible = True
            self._pending_detail_floating = False
            self._pending_detail_geometry = None
            self._reset_sections_to_defaults()
        else:
            self._pending_dock_width = max(220, min(width, 900))
            self._pending_dock_visible = bool(self.settings_data.get("control_dock_visible", True))
            self._pending_dock_floating = bool(self.settings_data.get("control_dock_floating", False))
            self._pending_dock_geometry = self.settings_data.get("control_dock_float_geometry")
            self._pending_detail_visible = bool(self.settings_data.get("detail_dock_visible", True))
            self._pending_detail_floating = bool(self.settings_data.get("detail_dock_floating", False))
            self._pending_detail_geometry = self.settings_data.get("detail_dock_float_geometry")
            encoded = self.settings_data.get("main_window_geometry")
            if isinstance(encoded, str) and encoded:
                try:
                    self.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii")))
                except Exception:
                    pass
        QTimer.singleShot(0, self._apply_restored_dock_state)

    def _apply_restored_dock_state(self):
        if self._pending_dock_floating:
            self.control_dock.setFloating(True)
            encoded = self._pending_dock_geometry
            if isinstance(encoded, str) and encoded:
                try:
                    self.control_dock.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii")))
                except Exception:
                    pass
        else:
            self.addDockWidget(Qt.RightDockWidgetArea, self.control_dock)
            self.control_dock.setFloating(False)
            try:
                self.resizeDocks([self.control_dock], [self._pending_dock_width], Qt.Horizontal)
            except Exception:
                pass

        self.control_dock.setVisible(self._pending_dock_visible)
        if self.control_dock.isVisible() and self.control_dock.isFloating():
            QTimer.singleShot(120, self.ensure_control_panel_visible)

        if self._pending_detail_floating:
            self.detail_dock.setFloating(True)
            encoded = self._pending_detail_geometry
            if isinstance(encoded, str) and encoded:
                try:
                    self.detail_dock.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii")))
                except Exception:
                    pass
        else:
            self.addDockWidget(Qt.RightDockWidgetArea, self.detail_dock)
            self.detail_dock.setFloating(False)
            try:
                self.splitDockWidget(self.control_dock, self.detail_dock, Qt.Vertical)
            except Exception:
                pass
        self.detail_dock.setVisible(self._pending_detail_visible)
        if self.detail_dock.isVisible() and self.detail_dock.isFloating():
            QTimer.singleShot(140, self.ensure_detail_panel_visible)

    def _on_control_dock_visibility_changed(self, visible: bool):
        # 顶部按钮始终是一个唤回入口，不作为第二个关闭按钮。
        if not visible:
            self.panel_toggle.setToolTip("控制面板已隐藏，点击重新显示")
        elif self.control_dock.isFloating():
            self.panel_toggle.setToolTip("控制面板正在浮动；点击可唤回到前台，箭头菜单可停靠到右侧")
        else:
            self.panel_toggle.setToolTip("控制面板已停靠在右侧；箭头菜单可切换为浮动")

    def _on_control_dock_top_level_changed(self, floating: bool):
        self._on_control_dock_visibility_changed(self.control_dock.isVisible())
        if floating:
            QTimer.singleShot(80, self.ensure_control_panel_visible)
            self._show_status_message("控制面板已浮动：可拖到其他显示器；拖回主窗口右侧可重新吸附。", 5000)
        else:
            self._show_status_message("控制面板已重新停靠到主窗口右侧。", 3500)

    def _on_control_dock_location_changed(self, area):
        # 当前只允许右侧停靠。此回调主要用于刷新入口提示。
        self._on_control_dock_visibility_changed(self.control_dock.isVisible())

    def show_control_panel(self):
        if not self.control_dock.isVisible():
            self.control_dock.show()
        if self.control_dock.isFloating():
            self.ensure_control_panel_visible()
        self.control_dock.raise_()
        try:
            self.control_dock.activateWindow()
            self.control_dock.setFocus(Qt.OtherFocusReason)
        except Exception:
            pass

    def dock_control_panel_right(self):
        # 这是浮动面板跑到其他屏幕、甚至难以拖回时最可靠的手工召回方式。
        if self.control_dock.isFloating():
            self.control_dock.setFloating(False)
        self.addDockWidget(Qt.RightDockWidgetArea, self.control_dock)
        self.control_dock.show()
        try:
            self.resizeDocks([self.control_dock], [max(260, min(self._pending_dock_width if hasattr(self, "_pending_dock_width") else 350, 700))], Qt.Horizontal)
        except Exception:
            pass
        self.control_dock.raise_()

    def float_control_panel(self):
        self.control_dock.show()
        if not self.control_dock.isFloating():
            self.control_dock.setFloating(True)
            # 第一次浮动时给一个适中的独立窗口尺寸；之后用户自行拖动/缩放。
            screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                width = min(max(360, self.control_dock.width()), max(360, avail.width() - 80))
                height = min(max(620, int(avail.height() * 0.82)), max(420, avail.height() - 80))
                self.control_dock.resize(width, height)
                self.control_dock.move(
                    avail.x() + max(20, avail.width() - width - 40),
                    avail.y() + 40,
                )
        self.control_dock.raise_()
        try:
            self.control_dock.activateWindow()
        except Exception:
            pass

    def hide_control_panel(self):
        self.control_dock.hide()

    def _on_detail_dock_visibility_changed(self, visible: bool):
        if not visible:
            self.detail_toggle.setToolTip("详情面板已隐藏，点击重新显示")
        elif self.detail_dock.isFloating():
            self.detail_toggle.setToolTip("详情面板正在浮动；点击可唤回到前台")
        else:
            self.detail_toggle.setToolTip("详情面板已停靠；箭头菜单可切换为浮动")

    def _on_detail_dock_top_level_changed(self, floating: bool):
        self._on_detail_dock_visibility_changed(self.detail_dock.isVisible())
        if floating:
            QTimer.singleShot(80, self.ensure_detail_panel_visible)

    def show_detail_panel(self):
        if not self.detail_dock.isVisible():
            self.detail_dock.show()
        if self.detail_dock.isFloating():
            self.ensure_detail_panel_visible()
        self.detail_dock.raise_()
        try:
            self.detail_dock.activateWindow()
        except Exception:
            pass

    def dock_detail_panel_right(self):
        if self.detail_dock.isFloating():
            self.detail_dock.setFloating(False)
        self.addDockWidget(Qt.RightDockWidgetArea, self.detail_dock)
        try:
            self.splitDockWidget(self.control_dock, self.detail_dock, Qt.Vertical)
        except Exception:
            pass
        self.detail_dock.show()
        self.detail_dock.raise_()

    def float_detail_panel(self):
        self.detail_dock.show()
        if not self.detail_dock.isFloating():
            self.detail_dock.setFloating(True)
            screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
            if screen is not None:
                avail = screen.availableGeometry()
                width = min(max(520, self.detail_dock.width()), max(420, avail.width() - 80))
                height = min(max(650, int(avail.height() * 0.85)), max(420, avail.height() - 80))
                self.detail_dock.resize(width, height)
                self.detail_dock.move(avail.x() + 50, avail.y() + 40)
        self.detail_dock.raise_()
        try:
            self.detail_dock.activateWindow()
        except Exception:
            pass

    def hide_detail_panel(self):
        self.detail_dock.hide()

    def ensure_detail_panel_visible(self):
        if not self.detail_dock.isVisible() or not self.detail_dock.isFloating():
            return
        frame = self.detail_dock.frameGeometry()
        screen, area = self._screen_with_largest_intersection(frame)
        if screen is not None and area > 0:
            return
        main_screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if main_screen is None:
            return
        avail = main_screen.availableGeometry()
        width = min(max(420, frame.width()), max(420, avail.width() - 40))
        height = min(max(420, frame.height()), max(420, avail.height() - 40))
        self.detail_dock.resize(width, height)
        self.detail_dock.move(avail.x() + max(0, (avail.width() - width) // 2), avail.y() + max(0, (avail.height() - height) // 2))

    def ensure_control_panel_visible(self):
        if not self.control_dock.isVisible() or not self.control_dock.isFloating():
            return
        frame = self.control_dock.frameGeometry()
        screen, area = self._screen_with_largest_intersection(frame)
        if screen is not None and area > 0:
            return

        # Qt 已明确判定面板不在任何现有屏幕时，将其拉回主窗口所在屏幕。
        main_screen = QGuiApplication.screenAt(self.frameGeometry().center()) or QGuiApplication.primaryScreen()
        if main_screen is None:
            return
        avail = main_screen.availableGeometry()
        width = min(max(320, frame.width()), max(320, avail.width() - 40))
        height = min(max(420, frame.height()), max(420, avail.height() - 40))
        self.control_dock.resize(width, height)
        self.control_dock.move(
            avail.x() + max(0, (avail.width() - width) // 2),
            avail.y() + max(0, (avail.height() - height) // 2),
        )

    # ───────────────── 窗口 / 多显示器安全 ─────────────────
    def _reset_sections_to_defaults(self):
        defaults = {
            "source": True,
            "status": True,
            "batch": True,
            "destination": False,
        }
        for key, expanded in defaults.items():
            section = self.sections.get(key)
            if section is not None:
                section.set_expanded(expanded)

    def _install_screen_watchers(self):
        app = QGuiApplication.instance()
        if app is None:
            return
        try:
            app.screenAdded.connect(self._on_screen_topology_changed)
            app.screenRemoved.connect(self._on_screen_topology_changed)
        except Exception:
            pass
        for screen in app.screens():
            self._watch_screen(screen)

    def _watch_screen(self, screen):
        try:
            screen.availableGeometryChanged.connect(self._on_screen_topology_changed)
            screen.geometryChanged.connect(self._on_screen_topology_changed)
        except Exception:
            pass

    def _on_screen_topology_changed(self, *args):
        # Windows 在关屏/改缩放/重新排列显示器时可能连续发送多次变化。
        # 分两次校正，避免第一次校正后系统又把窗口挪走。
        if args:
            screen = args[0]
            if hasattr(screen, "availableGeometry"):
                self._watch_screen(screen)
        QTimer.singleShot(250, self.ensure_main_window_visible)
        QTimer.singleShot(1100, self.ensure_main_window_visible)
        QTimer.singleShot(300, self.ensure_control_panel_visible)
        QTimer.singleShot(1150, self.ensure_control_panel_visible)
        QTimer.singleShot(340, self.ensure_detail_panel_visible)
        QTimer.singleShot(1190, self.ensure_detail_panel_visible)

    def _cursor_screen(self):
        screen = QGuiApplication.screenAt(QCursor.pos())
        if screen is None:
            screen = QGuiApplication.primaryScreen()
        return screen

    def _screen_with_largest_intersection(self, rect):
        best_screen = None
        best_area = -1
        for screen in QGuiApplication.screens():
            avail = screen.availableGeometry()
            inter = rect.intersected(avail)
            area = max(0, inter.width()) * max(0, inter.height())
            if area > best_area:
                best_area = area
                best_screen = screen
        return best_screen, best_area

    def _clamp_window_to_screen(self, screen, center_if_lost: bool = False):
        if screen is None:
            return
        avail = screen.availableGeometry()
        if self.isMaximized() or self.isFullScreen():
            # 最大化窗口由 Qt/Windows 自己适配当前屏幕。
            return
        frame = self.frameGeometry()
        width = min(max(520, frame.width()), max(520, avail.width() - 24))
        height = min(max(420, frame.height()), max(420, avail.height() - 24))
        self.resize(width, height)

        if center_if_lost:
            x = avail.x() + max(0, (avail.width() - width) // 2)
            y = avail.y() + max(0, (avail.height() - height) // 2)
        else:
            # 保证标题栏与左右边界都回到可操作区域。
            x = min(max(frame.x(), avail.left() + 8), avail.right() - width - 8)
            y = min(max(frame.y(), avail.top() + 8), avail.bottom() - height - 8)
        self.move(x, y)

    def ensure_main_window_visible(self):
        if not self.isVisible():
            return
        frame = self.frameGeometry()
        screen, area = self._screen_with_largest_intersection(frame)
        title_height = min(44, max(1, frame.height()))
        title_rect = frame.adjusted(0, 0, 0, -(frame.height() - title_height))
        title_visible = False
        for s in QGuiApplication.screens():
            if title_rect.intersects(s.availableGeometry()):
                title_visible = True
                break

        if screen is None or area <= 0 or not title_visible:
            # 已经跑出所有可见屏幕，优先拉到鼠标所在屏幕。
            screen = self._cursor_screen()
            self._clamp_window_to_screen(screen, center_if_lost=True)
            self._show_status_message("检测到窗口位置不可操作，已自动拉回当前可见屏幕。", 5000)
        else:
            # 即使还有一点可见，也保证标题栏可以被鼠标拖到。
            self._clamp_window_to_screen(screen, center_if_lost=False)

    def _apply_startup_window_safety(self):
        if self.safe_layout:
            self.reset_default_layout(show_message=False)
        else:
            self.ensure_main_window_visible()

    def reset_default_layout(self, show_message: bool = True):
        screen = self._cursor_screen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        self.showNormal()
        width = min(1420, max(720, avail.width() - 40))
        height = min(900, max(620, avail.height() - 40))
        self.resize(width, height)
        self.move(
            avail.x() + max(0, (avail.width() - width) // 2),
            avail.y() + max(0, (avail.height() - height) // 2),
        )
        if self.control_dock.isFloating():
            self.control_dock.setFloating(False)
        self.addDockWidget(Qt.RightDockWidgetArea, self.control_dock)
        self.control_dock.show()
        if self.detail_dock.isFloating():
            self.detail_dock.setFloating(False)
        self.addDockWidget(Qt.RightDockWidgetArea, self.detail_dock)
        try:
            self.splitDockWidget(self.control_dock, self.detail_dock, Qt.Vertical)
        except Exception:
            pass
        self.detail_dock.show()
        try:
            self.resizeDocks([self.control_dock, self.detail_dock], [350, 350], Qt.Horizontal)
            self.resizeDocks([self.control_dock, self.detail_dock], [430, 430], Qt.Vertical)
        except Exception:
            pass
        self._reset_sections_to_defaults()

        # 只重置窗口布局相关配置；数据库路径、工作目录、会话和人工修改全部保留。
        for key in [
            "main_window_geometry", "control_dock_width", "control_dock_visible",
            "control_dock_floating", "control_dock_float_geometry",
            "control_sections", "main_splitter_sizes",
            "detail_dock_visible", "detail_dock_floating", "detail_dock_float_geometry",
        ]:
            self.settings_data.pop(key, None)
        self._save_settings_file()
        self.raise_()
        self.activateWindow()
        if show_message:
            self._show_status_message("已恢复默认布局；任务、人工修改和路径设置均未改变。", 5000)

    # ───────────────── 快照清理 ─────────────────
    def _cleanup_old_snapshots(self):
        """清理未被会话/历史引用的旧快照；任何清理失败都不阻断主程序。"""
        try:
            tool = Path(self.tool_dir())
            result = cleanup_snapshots(
                tool / "snapshots",
                reference_roots=[tool / "sessions", tool / "history"],
                retention_days=7,
                max_unreferenced_per_day=5,
            )
            deleted = result.get("deleted", [])
            if deleted:
                log_message(f"快照自动清理：删除 {len(deleted)} 份未引用旧快照。")
        except Exception as exc:
            log_message(f"快照自动清理失败（已忽略）：{exc}", "WARNING")

    # ───────────────── 会话保存 / 恢复 ─────────────────
    def sessions_dir(self) -> Path:
        path = Path(self.tool_dir()) / "sessions"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def session_path(self) -> Path:
        return self.sessions_dir() / "current_session.json"

    def session_archive_dir(self) -> Path:
        path = self.sessions_dir() / "archive"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def checkpoint_dir(self) -> Path:
        path = self.sessions_dir() / "checkpoints"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _library_lock_active(self) -> bool:
        return bool(getattr(self, "library_lock_check", None) and self.library_lock_check.isChecked())

    def _refresh_library_lock_ui(self):
        active = self.active_library_tasks()
        locked_for_change = active or (self._library_lock_active() and bool(self.all_items))
        if hasattr(self, "db_edit"):
            self.db_edit.setReadOnly(locked_for_change)
        if hasattr(self, "db_choose_btn"):
            self.db_choose_btn.setEnabled(not locked_for_change)
        if hasattr(self, 'work_edit'):
            self.work_edit.setReadOnly(active)
        if hasattr(self, 'work_choose_btn'):
            self.work_choose_btn.setEnabled(not active)
        if hasattr(self, 'scan_btn'):
            self.scan_btn.setEnabled(not active)
        if hasattr(self, 'scan_depth_spin'):
            self.scan_depth_spin.setEnabled(not active)

    def _on_library_lock_toggled(self, checked: bool):
        if not checked and self.all_items:
            answer = QMessageBox.question(
                self,
                "解除工作库保护",
                "当前任务已有工作状态。解除后可以更换数据库。\n\n"
                "更换数据源前仍建议先创建检查点。是否解除保护？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                self.library_lock_check.blockSignals(True)
                self.library_lock_check.setChecked(True)
                self.library_lock_check.blockSignals(False)
                checked = True
        self.settings_data["library_lock"] = bool(checked)
        self._save_settings_file()
        self._refresh_library_lock_ui()

    def create_manual_checkpoint(self, show_message: bool = True):
        if not self.all_items:
            if show_message:
                QMessageBox.information(self, "没有当前任务", "当前没有可保存的任务。")
            return None
        self._save_current_session()
        try:
            path = create_checkpoint(self.session_path(), self.checkpoint_dir())
        except Exception as exc:
            if show_message:
                QMessageBox.warning(self, "检查点失败", f"无法创建检查点：\n\n{exc}")
            return None
        if path:
            self._show_status_message(f"检查点已创建：{path.name}", 5000)
            if show_message:
                QMessageBox.information(
                    self, "检查点已创建",
                    f"检查点已保存。\n\n文件：{path.name}\n位置：sessions/checkpoints"
                )
        return path

    def _checkpoint_summary(self, path: Path) -> str:
        try:
            data = load_session(path) or {}
            items = data.get("items", []) if isinstance(data.get("items"), list) else []
            manual = sum(
                1 for x in items
                if isinstance(x, dict) and (
                    x.get("manual_name") or x.get("manual_edited") or x.get("manual_attribute_overrides")
                )
            )
            confirmed = sum(1 for x in items if isinstance(x, dict) and x.get("manual_confirmed"))
            ai = sum(1 for x in items if isinstance(x, dict) and x.get("ai_status") == AI_REVIEWED)
            saved = str(data.get("saved_at", "-")).replace("T", " ")
            work = str(data.get("work_directory", "")) or "-"
            return (
                f"保存时间：{saved}\n项目数量：{len(items)}\n人工修改：{manual}\n"
                f"人工确认：{confirmed}\nAI已审：{ai}\n工作目录：{work}"
            )
        except Exception as exc:
            return f"无法读取检查点：{exc}"

    def open_checkpoint_manager(self):
        dialog = QDialog(self)
        dialog.setWindowTitle("检查点管理")
        dialog.resize(760, 500)
        root = QVBoxLayout(dialog)
        hint = QLabel("检查点保存软件工作状态。恢复检查点前会先自动保存当前状态，不会直接回滚硬盘文件。")
        hint.setWordWrap(True)
        root.addWidget(hint)

        middle = QHBoxLayout()
        checkpoint_list = QListWidget()
        detail = QPlainTextEdit()
        detail.setReadOnly(True)
        detail.setPlaceholderText("选择左侧检查点查看详情")
        middle.addWidget(checkpoint_list, 2)
        middle.addWidget(detail, 3)
        root.addLayout(middle, 1)

        def reload_list(select_path: Path | None = None):
            checkpoint_list.clear()
            files = sorted(self.checkpoint_dir().glob("checkpoint_*.json"), key=lambda x: x.stat().st_mtime, reverse=True)
            select_row = -1
            for i, path in enumerate(files):
                try:
                    data = load_session(path) or {}
                    label = str(data.get("saved_at", "")).replace("T", " ") or path.stem.replace("checkpoint_", "")
                    count = len(data.get("items", [])) if isinstance(data.get("items"), list) else 0
                    text = f"{label}　({count} 项)"
                except Exception:
                    text = path.name
                item = QListWidgetItem(text)
                item.setData(Qt.UserRole, str(path))
                checkpoint_list.addItem(item)
                if select_path is not None and path == select_path:
                    select_row = i
            if checkpoint_list.count():
                checkpoint_list.setCurrentRow(select_row if select_row >= 0 else 0)
            else:
                detail.setPlainText("尚无检查点。可点击下方“创建检查点”。")

        def selected_path() -> Path | None:
            item = checkpoint_list.currentItem()
            if item is None:
                return None
            value = item.data(Qt.UserRole)
            return Path(str(value)) if value else None

        def update_detail():
            path = selected_path()
            detail.setPlainText(self._checkpoint_summary(path) if path else "")

        checkpoint_list.currentItemChanged.connect(lambda *_: update_detail())

        buttons = QHBoxLayout()
        create_btn = QPushButton("创建检查点")
        restore_btn = QPushButton("恢复选中检查点")
        delete_btn = QPushButton("删除选中检查点")
        close_btn = QPushButton("关闭")
        buttons.addWidget(create_btn)
        buttons.addWidget(restore_btn)
        buttons.addWidget(delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        root.addLayout(buttons)

        def create_here():
            path = self.create_manual_checkpoint(show_message=False)
            if path:
                reload_list(path)
                self._show_status_message(f"检查点已创建：{path.name}", 4000)

        def restore_here():
            path = selected_path()
            if not path:
                QMessageBox.information(dialog, "没有选择", "请选择要恢复的检查点。")
                return
            answer = QMessageBox.question(
                dialog, "恢复检查点",
                "恢复会用检查点中的工作状态替换当前界面状态。\n恢复前会自动给当前状态再创建一份检查点。\n\n是否继续？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            if self.all_items:
                self.create_manual_checkpoint(show_message=False)
            try:
                data = load_session(path)
                if not data:
                    raise ValueError("检查点格式无效或内容为空")
                if not self._restore_session_data(data):
                    return
                self._save_current_session()
                dialog.accept()
                self._show_status_message(f"已恢复检查点：{path.name}", 5000)
            except Exception as exc:
                QMessageBox.warning(dialog, "恢复失败", str(exc))

        def delete_here():
            path = selected_path()
            if not path:
                return
            answer = QMessageBox.question(
                dialog, "删除检查点", f"确定删除检查点 {path.name}？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer == QMessageBox.StandardButton.Yes:
                try:
                    path.unlink(missing_ok=True)
                    reload_list()
                except Exception as exc:
                    QMessageBox.warning(dialog, "删除失败", str(exc))

        create_btn.clicked.connect(create_here)
        restore_btn.clicked.connect(restore_here)
        delete_btn.clicked.connect(delete_here)
        close_btn.clicked.connect(dialog.accept)
        reload_list()
        update_detail()
        dialog.exec()

    def _schedule_session_save(self):
        if self.all_items:
            if self._perf_diag:
                self._perf_diag.record(
                    "session.schedule", pending=int(self.session_save_timer.isActive())
                )
            self.session_save_timer.start()

    def _session_snapshot_dict(self):
        return asdict(self.snapshot_info) if self.snapshot_info is not None else None

    _write_session_snapshot = staticmethod(write_frozen_session)

    def _start_background_session_save(self, path: Path, frozen_snapshot: bytes):
        if self._session_save_executor is None:
            self._session_save_executor = ProcessPoolExecutor(
                max_workers=1, mp_context=get_context("spawn")
            )
        self._background_session_save = self._session_save_executor.submit(
            self._write_session_snapshot, path, frozen_snapshot
        )
        self._session_save_poll.start()

    def _poll_background_session_save(self):
        future = self._background_session_save
        if future is None or not future.done():
            return
        self._background_session_save = None
        try:
            elapsed_ms = future.result()
            if self._ai_runner:
                self._ai_log(self._ai_runner.task_id).event("background_session_saved", save_ms=elapsed_ms)
            if self._perf_diag:
                self._perf_diag.record("session.background_io", elapsed_ms)
            if self._pending_session_snapshot is None:
                self.session_label.setText(f"会话：已自动保存 {datetime.now().strftime('%H:%M:%S')}")
        except Exception as exc:
            self.session_label.setText(f"会话：保存失败（{_redact(str(exc))}）")
            log_exception(type(exc), exc, exc.__traceback__, "后台会话保存失败")
            if self._ai_runner:
                self._ai_log(self._ai_runner.task_id).exception("session_save_failed", exc,
                    stage="后台保存", write_stage=getattr(exc, "session_write_stage", "未提供"))
            if self._perf_diag:
                self._perf_diag.record("session.error", error_type=type(exc).__name__)
        pending = self._pending_session_snapshot
        self._pending_session_snapshot = None
        if pending is None:
            self._session_save_poll.stop()
        else:
            self._start_background_session_save(*pending)

    def _finish_background_session_save(self):
        # Explicit saves and shutdown must not be followed by an older timed write.
        self.session_save_timer.stop()
        self._pending_session_snapshot = None
        future = self._background_session_save
        if future is not None:
            if not future.done():
                try:
                    future.result()  # No GUI re-entry while the older snapshot finishes.
                except Exception:
                    pass  # The completed write is reported below; the direct save still runs.
            self._poll_background_session_save()
        self._session_save_poll.stop()

    def _save_current_session(self, strict: bool = False, *, stage: str = "会话保存", task_id: str | None = None):
        started = perf_counter()
        if task_id is None and self._ai_runner:
            task_id = self._ai_runner.task_id
        try:
            result = self._save_current_session_impl(strict)
            if task_id:
                queued = not strict and self.sender() is self.session_save_timer
                self._ai_log(task_id).event(("session_queued" if queued else "session_saved") if result else "session_not_saved",
                    stage=stage, save_ms=round((perf_counter() - started) * 1000, 2), saved=bool(result))
            return result
        except Exception as exc:
            log_exception(type(exc), exc, exc.__traceback__, f"会话保存失败：{stage}")
            if task_id:
                self._ai_log(task_id).exception("session_save_failed", exc, stage=stage,
                    write_stage=getattr(exc, "session_write_stage", "构建或提交会话"),
                    save_ms=round((perf_counter() - started) * 1000, 2))
            raise

    def _save_current_session_impl(self, strict: bool = False):
        if not self.all_items:
            if strict:
                raise OSError("没有可保存的当前漫画任务")
            return False
        timed_save = not strict and self.sender() is self.session_save_timer
        if not timed_save:
            self._finish_background_session_save()
        perf = self._perf_diag
        started = perf_counter() if perf else 0
        stage = started
        if perf:
            perf.record(
                "session.begin",
                trigger="timer" if timed_save else "direct",
                item_count=len(self.all_items),
            )
        self._commit_active_edit()
        if perf:
            stage = perf.mark("session.commit_edit", stage)
        self._session_revision = max(getattr(self, "_session_revision", 0) + 1, time_ns())
        saved_items = [item_to_dict(item) for item in self.all_items]
        for original_position, raw_item in self._missing_session_items:
            saved_items.insert(min(original_position, len(saved_items)), raw_item)
        if perf:
            stage = perf.mark("session.items_to_dict", stage, item_count=len(saved_items))
        payload = {
            "app_version": VERSION,
            "library_id": self._ensure_library(),
            "session_id": self.session_id,
            "session_revision": self._session_revision,
            "ai_tasks": self.ai_tasks,
            "source_database": (self._loaded_source or (self.db_edit.text().strip(), ''))[0],
            "work_directory": (self._loaded_source or ('', self.work_edit.text().strip()))[1],
            "scan_max_depth": self.scan_depth_spin.value(),
            "destination_directory": self.dest_edit.text().strip(),
            "snapshot_info": self._session_snapshot_dict(),
            "pagination": {
                "category_pages": {
                    category: int(self.category_pages.get(category, 1))
                    for category in CATEGORIES
                },
                "last_category": self._last_ordinary_category,
            },
            "items": saved_items,
        }
        if perf:
            stage = perf.mark("session.build_payload", stage)
        try:
            path = self.session_path()
            if timed_save:
                # Freeze every nested field before the UI can change it while
                # the background writer serializes the session to JSON.
                frozen_snapshot = pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL)
                if perf:
                    stage = perf.mark("session.freeze_snapshot", stage)
                if self._background_session_save is None:
                    self._start_background_session_save(path, frozen_snapshot)
                else:
                    self._pending_session_snapshot = (path, frozen_snapshot)
                if perf:
                    stage = perf.mark("session.enqueue", stage)
            else:
                save_session(path, payload)
                if perf:
                    stage = perf.mark("session.json_and_disk", stage)
                self.session_label.setText(f"会话：已自动保存 {datetime.now().strftime('%H:%M:%S')}")
        except Exception as exc:
            self.session_label.setText(f"会话：保存失败（{_redact(str(exc))}）")
            if perf:
                perf.record("session.error", error_type=type(exc).__name__)
            if strict:
                raise
            log_exception(type(exc), exc, exc.__traceback__, "普通会话保存失败")
            if self._ai_runner:
                self._ai_log(self._ai_runner.task_id).exception("session_save_failed", exc,
                    stage="普通会话保存", write_stage=getattr(exc, "session_write_stage", "构建或提交会话"))
            return False
        finally:
            if perf:
                perf.mark("session.total", started, item_count=len(self.all_items))
        return True

    def _manual_work_count(self) -> int:
        return sum(
            1 for item in self.all_items
            if item.has_manual_version or item.manual_edited or item.manual_category
            or item.manual_confirmed or item.ai_status != AI_UNREVIEWED or item.manual_attribute_overrides
        )

    def _recoverable_session(self) -> dict | None:
        try:
            data = self.txt_repository().latest(load_session(self.session_path()))
            if not data or not isinstance(data.get("items"), list) or not data["items"]:
                return None
            work_dir = str(data.get("work_directory", "")).strip()
            if work_dir and not os.path.isdir(work_dir):
                return None
            if not any(
                isinstance(raw, dict) and raw.get("original_path")
                and os.path.exists(raw["original_path"])
                for raw in data["items"]
            ):
                return None
            return data
        except (OSError, ValueError, TypeError):
            return None

    def _refresh_empty_state(self):
        if self.all_items:
            self.main_stack.setCurrentWidget(self.tabs)
            self.pager_widget.show()
            return
        self.main_stack.setCurrentWidget(self.empty_state)
        self.pager_widget.hide()
        data = self._recoverable_session()
        self.restore_previous_btn.setVisible(data is not None)
        if data is not None:
            saved = str(data.get("saved_at", "")).replace("T", " ")
            self.restore_previous_info.setText(f"上次任务：{len(data['items'])} 项" + (f" · {saved}" if saved else ""))
        else:
            self.restore_previous_info.clear()

    def restore_previous_task(self):
        if self._block_library_change():
            return
        if self.all_items:
            return
        data = self._recoverable_session()
        if data is not None:
            self._restore_session_data(data)
        self._refresh_empty_state()

    def _offer_restore_and_file_tasks(self):
        self._offer_restore_session()
        self._offer_file_recovery()

    def _offer_restore_session(self):
        try:
            data = self.txt_repository().latest(load_session(self.session_path()))
        except Exception as exc:
            self.session_label.setText(f"会话：读取失败（{exc}）")
            self._refresh_empty_state()
            return
        self._refresh_empty_state()
        if not data or self.all_items:
            return

        items_data = data.get("items", [])
        if not isinstance(items_data, list) or not items_data:
            return
        manual_count = sum(
            1 for x in items_data
            if isinstance(x, dict) and (
                x.get("manual_name") or x.get("manual_edited") or x.get("manual_category")
                or x.get("manual_confirmed") or x.get("ai_status") in {AI_REVIEWED, AI_FAILED} or x.get("manual_attribute_overrides")
            )
        )
        saved_at = str(data.get("saved_at", "未知时间")).replace("T", " ")
        work_dir = str(data.get("work_directory", ""))

        box = QMessageBox(self)
        box.setWindowTitle("发现未完成的上次任务")
        box.setIcon(QMessageBox.Icon.Information)
        box.setText(
            f"检测到上次自动保存的任务。\n\n"
            f"保存时间：{saved_at}\n"
            f"工作目录：{work_dir or '(未记录)'}\n"
            f"项目数量：{len(items_data)}\n"
            f"人工修改/确认/状态：{manual_count}\n\n"
            "勾选状态不会恢复，避免重新打开后误执行。"
        )
        continue_btn = box.addButton("继续上次任务", QMessageBox.ButtonRole.AcceptRole)
        later_btn = box.addButton("暂不载入", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(continue_btn)
        box.exec()

        if box.clickedButton() is continue_btn:
            self._restore_session_data(data)
        else:
            # 暂不载入时保留会话，用户下次启动仍可继续。
            self.session_label.setText("会话：存在未载入的上次任务")
        self._refresh_empty_state()

    def _restore_session_data(self, data: dict) -> bool:
        if self._block_library_change():
            return False
        try:
            self.txt_repository().scan()
            data = self.txt_repository().preserve_review_history(data)
            from .ai_txt_store import check_task_record, is_txt
            for task in data.get("ai_tasks", []) if isinstance(data.get("ai_tasks"), list) else []:
                if is_txt(task):
                    check_task_record(task)
                    if task.get("session_id") != data.get("session_id"):
                        raise ValueError("TXT任务与原会话身份不一致，无法安全恢复")
            raw_items = data.get("items", [])
            parsed_items = [
                (position, item_from_dict(x))
                for position, x in enumerate(raw_items) if isinstance(x, dict)
            ]
            items = [item for _, item in parsed_items]
            database = str(data.get('source_database', ''))
            directory = str(data.get('work_directory', ''))
            from .library_identity import LibraryRegistry
            restored_library_id = LibraryRegistry(self.tool_dir()).resolve(database, directory, str(data.get('library_id') or ''))
            self._recover_file_items(items, restored_library_id, str(data.get('session_id') or ''), True)
            # Retain the recovered objects in the positional restore list as well.
            parsed_items = [(position, items[index]) for index, (position, _) in enumerate(parsed_items)]
            saved_tasks = data.get("ai_tasks")
            if isinstance(saved_tasks, list) and saved_tasks and len({x.local_id for x in items}) != len(items):
                raise ValueError("带有 AI 任务的会话存在重复漫画编号，不能安全恢复匹配")
            for item in items:
                if item.category not in CATEGORIES:
                    item.category = CATEGORY_REVIEW
                item.checked = False
            if not items:
                raise ValueError("会话中没有可恢复的项目")

            work_directory = str(data.get("work_directory", "")).strip()
            if work_directory and not os.path.isdir(work_directory):
                QMessageBox.warning(
                    self,
                    "原工作目录不可用",
                    "上次任务的原工作目录当前不存在或不可访问，本次不会恢复任务。\n\n"
                    "会话记录已保留；硬盘重新挂载后可再次尝试。\n\n"
                    f"工作目录：{work_directory}",
                )
                self.session_label.setText("会话：原工作目录不可用，旧会话已保留")
                return False

            valid_items: list[WorkItem] = []
            missing_items: list[tuple[int, dict]] = []
            skipped_missing = 0
            for position, item in parsed_items:
                try:
                    exists = bool(item.original_path and os.path.exists(item.original_path))
                except OSError:
                    exists = False
                if exists:
                    valid_items.append(item)
                else:
                    skipped_missing += 1
                    missing_items.append((position, raw_items[position]))
            if not valid_items:
                QMessageBox.warning(
                    self,
                    "没有可恢复的项目",
                    "会话中的漫画路径已经全部失效，本次不恢复任务。\n\n"
                    "不会用同名文件夹、当前行号或其他项目替代。旧会话记录仍然保留。",
                )
                self.session_label.setText("会话：全部项目路径失效，旧会话已保留")
                return False
            items = valid_items

            if self.all_items and data.get("session_id") != self.session_id and any(t.get("transport") == "TXT" for t in self.ai_tasks):
                self._save_current_session(strict=True, stage="恢复前保留旧TXT任务")
                from .ai_txt_store import TxtSession
                self.txt_repository().save_inactive(TxtSession(load_session(self.session_path())))

            self.db_edit.setText(str(data.get("source_database", "")))
            self.work_edit.setText(work_directory)
            try:
                self.scan_depth_spin.setValue(int(data.get("scan_max_depth", self.settings_data.get("scan_max_depth", 2))))
            except Exception:
                self.scan_depth_spin.setValue(2)
            self.dest_edit.setText(str(data.get("destination_directory", "")))

            snap = data.get("snapshot_info")
            self.snapshot_info = DatabaseInfo(**snap) if isinstance(snap, dict) else None
            self.records = [item.record for item in items if item.record is not None]
            # Undo/Redo history is intentionally runtime-only and belongs to the
            # previously loaded task. Session/checkpoint restore starts a fresh history.
            self._clear_undo_history()
            self.all_items = items
            self.library_id = restored_library_id
            self._loaded_source = (database, directory)
            self.session_id = str(data.get("session_id") or uuid.uuid4().hex)
            self._session_revision = int(data.get("session_revision", 0))
            raw_tasks = data.get("ai_tasks")
            self.ai_tasks = raw_tasks if isinstance(raw_tasks, list) else []
            for task in self.ai_tasks:
                task.setdefault('library_id', self.library_id)
                if task.get("state") in {"running", "queued"}:
                    task["state"] = "interrupted"
                for row in task.get("items", {}).values():
                    if row.get("state") == "dispatching":
                        row["state"] = "uncertain"
                        row["reason"] = "上次发送后程序中断，未能确认实际送达或有效结果；需明确接续"
            self._missing_session_items = missing_items
            pagination = data.get("pagination") if isinstance(data.get("pagination"), dict) else {}
            raw_pages = pagination.get("category_pages") if isinstance(pagination.get("category_pages"), dict) else {}
            self.category_pages = {}
            for category in CATEGORIES:
                try:
                    self.category_pages[category] = max(1, int(raw_pages.get(category, 1)))
                except (TypeError, ValueError):
                    self.category_pages[category] = 1
            restored_category = str(pagination.get("last_category", CATEGORIES[0]))
            if restored_category not in CATEGORIES:
                restored_category = CATEGORIES[0]
            self._last_ordinary_category = restored_category
            self._pre_search_category = restored_category
            self._reset_search_state(restored_category)
            # 旧版本保存的会话恢复时，自动用当前分析器更新“程序建议”；
            # 手动名称与人工分类仍由 analyze_items 的保留逻辑保护。
            analyze_items(self.all_items, preserve_manual_category=True, options=self._analysis_options())
            self.populate_categories(clear_checked=True)
            self._suppress_tab_change = True
            try:
                self.tabs.setCurrentWidget(self.views[restored_category])
            finally:
                self._suppress_tab_change = False
            self._active_scope_key = restored_category
            self._update_pager()
            self.reanalyze_action.setEnabled(bool(self.all_items))

            if self.snapshot_info:
                stamp = self.snapshot_info.snapshot_created_at.replace("T", " ")
                self.snapshot_label.setText(
                    f"恢复的快照：{stamp}\n{self.snapshot_info.record_count} 条 ｜ ID {self.snapshot_info.fingerprint}"
                )
            else:
                self.snapshot_label.setText("已恢复会话（未记录快照信息）")
            self.show_scan_summary()
            self._schedule_session_save()
            self._refresh_library_lock_ui()
            self.session_label.setText("会话：已恢复；后续修改会继续自动保存")
            self._show_status_message(
                f"已恢复上次任务｜{len(items)} 项｜勾选状态已清空"
                + (f"｜{skipped_missing} 项因路径不存在已跳过。" if skipped_missing else "。")
            )
            if skipped_missing:
                QMessageBox.information(
                    self,
                    "部分项目未恢复",
                    f"有 {skipped_missing} 项因实际路径不存在而跳过。\n\n"
                    "这些项目不会显示，也不会参与分类、搜索、导出或批量操作。",
                )
            return True
        except Exception as exc:
            QMessageBox.warning(self, "会话恢复失败", f"无法恢复上次任务：\n\n{exc}")
            return False

    def _on_item_changed(self, item: WorkItem):
        self._invalidate_execution_validation()
        if self._search_query:
            anchor = self._capture_page_anchor()
            self.populate_categories(anchor=anchor, preserve_current=True)
            self._schedule_session_save()
            return
        # 搜索总览与真实分类共享同一个 WorkItem；任一视图编辑后同步刷新其它模型。
        for model in self.models.values():
            model.refresh_item(item)
        if self.search_model is not None:
            self.search_model.refresh_item(item)
        current = self.current_view().currentIndex()
        if current.isValid() and current.data(CardListModel.ITEM_ROLE) is item:
            self.show_item_details(item)
        self._schedule_session_save()

    def _on_manual_state_changed(self, item: WorkItem):
        """输入过程中只刷新名称版本按钮，不重绘卡片，避免编辑光标跳动。"""
        current = self.current_view().currentIndex()
        if current.isValid() and current.data(CardListModel.ITEM_ROLE) is item:
            self._update_name_version_button(item)

    # ───────────────── 路径选择 ─────────────────
    def choose_database(self):
        if self._block_library_change():
            return
        if self._library_lock_active() and self.all_items:
            QMessageBox.information(
                self, "工作库已锁定",
                "当前任务的数据源已被“工作库保护”锁定。\n\n请先创建检查点，再主动解除保护后才能更换数据库。"
            )
            return
        start = self.db_edit.text().strip() or str(Path.home())
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 database.sqlite", start,
            "SQLite 数据库 (*.sqlite *.db);;所有文件 (*)"
        )
        if path:
            self.db_edit.setText(path)
            self._set_setting("source_database", path)

    def choose_work_dir(self):
        if self._block_library_change():
            return
        start = self.work_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择本批工作目录", start)
        if path:
            self.work_edit.setText(path)
            self._set_setting("work_directory", path)

    def choose_dest_dir(self):
        start = self.dest_edit.text().strip() or self.work_edit.text().strip() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "选择修改后移动目录", start)
        if path:
            self.dest_edit.setText(path)
            self._set_setting("destination_directory", path)

    def tool_dir(self) -> str:
        return str(data_dir())

    def snapshot_dir(self) -> str:
        path = os.path.join(self.tool_dir(), "snapshots")
        os.makedirs(path, exist_ok=True)
        return path

    # ───────────────── 扫描 ─────────────────
    def scan_batch(self):
        if self._block_library_change():
            return
        source_db = self.db_edit.text().strip()
        work_dir = self.work_edit.text().strip()
        from .file_tasks import identity_key
        prior_library = self.library_id
        current_file_snapshots = {identity_key(i.file_identity): item_to_dict(i)
                                  for i in self.all_items if i.file_identity}
        if not self.all_items:
            # A fresh scan after choosing "later" can still use the latest saved work.
            try:
                saved = load_session(self.session_path()) or {}
            except (OSError, ValueError, TypeError):
                # A damaged optional snapshot must not prevent a fresh disk scan.
                saved = {}
            prior_library = saved.get('library_id', '')
            current_file_snapshots = {identity_key(raw['file_identity']): raw
                for raw in saved.get('items', []) if isinstance(raw, dict) and raw.get('file_identity')}

        if self._library_lock_active() and self.all_items and self.snapshot_info:
            old_db = os.path.normcase(os.path.abspath(self.snapshot_info.source_path or ""))
            new_db = os.path.normcase(os.path.abspath(source_db or ""))
            if old_db and new_db and old_db != new_db:
                QMessageBox.warning(self, "工作库已锁定", "当前任务禁止切换到新的数据库。请先创建检查点并解除工作库保护。")
                return

        ok, message = validate_database(source_db)
        if not ok:
            QMessageBox.warning(self, "数据库不可用", message)
            return
        if not os.path.isdir(work_dir):
            QMessageBox.warning(self, "工作目录不可用", "请选择存在的本批工作目录。")
            return

        manual_count = self._manual_work_count()
        if self.all_items and (manual_count or self.ai_tasks):
            box = QMessageBox(self)
            box.setWindowTitle("当前任务存在人工修改")
            box.setText(f"当前任务有 {manual_count} 项人工修改、确认或审查状态。\n\n重新扫描会开始一份新任务。当前会话将先自动归档，不会直接丢失。")
            proceed = box.addButton("继续重新扫描", QMessageBox.AcceptRole)
            cancel = box.addButton("取消", QMessageBox.RejectRole)
            box.setDefaultButton(cancel)
            box.exec()
            if box.clickedButton() is not proceed:
                return
            if any(task.get("transport") == "TXT" for task in self.ai_tasks):
                try:
                    self._save_current_session(strict=True, stage="保留旧TXT任务")
                    from .ai_txt_store import TxtSession
                    original = load_session(self.session_path())
                    self.txt_repository().save_inactive(TxtSession(original))
                except (OSError, ValueError) as exc:
                    QMessageBox.warning(self, "旧TXT任务未确认保存", f"暂不重新扫描，请先保住旧任务和等待结果记录：{exc}")
                    return
            else:
                self._save_current_session()
            try:
                archive_session(self.session_path(), self.session_archive_dir())
            except Exception:
                pass

        self.scan_btn.setEnabled(False)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        self._show_status_message("正在建立数据库快照并扫描本批……", 0)
        try:
            self._cleanup_old_snapshots()
            self.snapshot_info = create_snapshot(source_db, self.snapshot_dir())
            self.records = load_records(self.snapshot_info.snapshot_path)
            entries = scan_work_directory(work_dir, max_depth=self.scan_depth_spin.value())
            self._reset_search_state(CATEGORIES[0])
            self._clear_undo_history()
            self.all_items = match_entries(entries, self.records)
            self.session_id = uuid.uuid4().hex
            self._bind_library(source_db, work_dir)
            self._recover_file_items(self.all_items, self.library_id, self.session_id,
                snapshots=current_file_snapshots if prior_library == self.library_id else None)
            self.ai_tasks = []
            analyze_items(self.all_items, preserve_manual_category=True, options=self._analysis_options())
            self._missing_session_items = []
            self.populate_categories(clear_checked=True, reset_pages=True)
            self.reanalyze_action.setEnabled(bool(self.all_items))
            self._set_setting("source_database", source_db)
            self._set_setting("work_directory", work_dir)
            self._set_setting("scan_max_depth", self.scan_depth_spin.value())

            stamp = self.snapshot_info.snapshot_created_at.replace("T", " ")
            self.snapshot_label.setText(
                f"快照：{stamp}\n{self.snapshot_info.record_count} 条 ｜ ID {self.snapshot_info.fingerprint}"
            )
            self.show_scan_summary()
            self._save_current_session()
            self._refresh_library_lock_ui()
            self._show_status_message(
                f"扫描完成｜本批 {len(self.all_items)} 项｜命名分析器 {ANALYZER_VERSION} 已运行；仅高把握规则会自动分流。"
            )
        except Exception as exc:
            QMessageBox.critical(self, "扫描失败", f"扫描过程中发生错误：\n\n{exc}")
            self._show_status_message("扫描失败。")
        finally:
            QApplication.restoreOverrideCursor()
            self.scan_btn.setEnabled(True)

    def reanalyze_current_task(self):
        if not self.all_items:
            QMessageBox.information(self, "没有当前任务", "请先扫描本批工作目录，或恢复上次任务。")
            return
        self._reanalyze_and_refresh(status_message=None)
        self._save_current_session()
        self.show_scan_summary()
        self._show_status_message(
            f"命名分析完成｜分析器 {ANALYZER_VERSION}｜人工/AI已完成状态均已保护。"
        )

    def _reanalyze_and_refresh(self, status_message: str | None = None):
        self._commit_active_edit()
        anchor = self._capture_page_anchor()
        for item in self.all_items:
            item.checked = False
        analyze_items(self.all_items, preserve_manual_category=True, options=self._analysis_options())
        self.populate_categories(
            anchor=anchor,
            clear_checked=False,
            force_search_first=bool(self._search_query),
        )
        if status_message:
            self._show_status_message(status_message, 5000)

    def populate_categories(
        self,
        *,
        anchor: PageAnchor | None = None,
        clear_checked: bool = False,
        force_search_first: bool = False,
        reset_pages: bool = False,
        preserve_current: bool = False,
        background_ai: bool = False,
    ):
        """Rebuild full category membership, then expose only page slices to models."""
        current_scope = self.current_category() if preserve_current else None
        current_item = self._current_item() if preserve_current else None
        current_id = current_item.local_id if current_item is not None else None
        if clear_checked:
            for item in self.all_items:
                item.checked = False
        for item in self.all_items:
            if item.category not in CATEGORIES:
                item.category = CATEGORY_REVIEW

        if self.ai_tasks and len({x.local_id for x in self.all_items}) != len(self.all_items):
            raise ValueError("带有 AI 任务的漫画编号重复，不能自动更换任务身份")
        repairs = ensure_unique_local_ids(self.all_items) if not self.ai_tasks else []
        if repairs:
            log_message(f"local_id 校验修复 {len(repairs)} 项：缺失/重复 ID 已安全重建。", "WARNING")
            self._show_status_message(
                f"检测到 {len(repairs)} 个缺失/重复 local_id，已生成新的唯一 ID。",
                7000,
            )

        self.category_items = group_and_sort(self.all_items)
        if reset_pages:
            self.category_pages = {category: 1 for category in CATEGORIES}
            self.search_page = 1

        old_search_ids = {x.local_id for x in self.search_results} if background_ai else set()
        if self._search_query:
            self.search_results = sort_items(
                item for item in self.all_items if self._item_matches_search(item, self._search_query)
            )
            if background_ai:
                new_ids = {x.local_id for x in self.search_results}
                for item in self.all_items:
                    if item.local_id in old_search_ids and item.local_id not in new_ids:
                        item.checked = False
        else:
            self.search_results = []

        for category in CATEGORIES:
            self.category_pages[category] = clamp_page(
                self.category_pages.get(category, 1),
                len(self.category_items.get(category, [])),
                self.page_size,
            )

        if force_search_first and self._search_query:
            self.search_page = 1
        elif anchor is not None:
            target_items = self._scope_items(anchor.scope)
            target_page = resolve_anchor_page(
                anchor.local_ids,
                anchor.page,
                target_items,
                self.page_size,
            )
            self._set_page_number_for_scope(anchor.scope, target_page)
        self.search_page = clamp_page(self.search_page, len(self.search_results), self.page_size)

        self._refresh_all_page_models()
        self.refresh_tab_titles()
        self._refresh_empty_state()
        self._clear_current_context(all_views=True)
        if current_scope is not None and current_id is not None:
            if background_ai:
                self._restore_current_on_visible_page(current_scope, current_id)
            else:
                self._restore_current_context(current_scope, current_id)
        self._update_pager()
        self.update_selected_count()

    def _category_tab_offset(self) -> int:
        return 1 if self.search_tab_active else 0

    def _set_search_tab_active(self, active: bool):
        active = bool(active)
        if active == self.search_tab_active or self.search_view is None:
            return
        self._suppress_tab_change = True
        try:
            if active:
                self.tabs.insertTab(0, self.search_view, f"{SEARCH_RESULTS_LABEL} {len(self.search_results)}")
                self.search_tab_active = True
            else:
                idx = self.tabs.indexOf(self.search_view)
                if idx >= 0:
                    self.tabs.removeTab(idx)
                self.search_tab_active = False
                if self.search_model is not None:
                    self.search_model.set_items([])
        finally:
            self._suppress_tab_change = False

    def refresh_tab_titles(self):
        offset = self._category_tab_offset()
        for category in CATEGORIES:
            i = CATEGORIES.index(category)
            total = len(self._scope_items(category))
            self.tabs.setTabText(i + offset, f"{category} {total}")
        if self.search_tab_active and self.search_view is not None:
            idx = self.tabs.indexOf(self.search_view)
            if idx >= 0:
                self.tabs.setTabText(idx, f"{SEARCH_RESULTS_LABEL} {len(self._scope_items(SEARCH_RESULTS_LABEL))}")

    def show_scan_summary(self):
        total = len(self.all_items)
        matched = sum(1 for x in self.all_items if x.record is not None)
        unmatched = sum(1 for x in self.all_items if x.category == "未匹配")
        errors = sum(1 for x in self.all_items if x.category == "异常")
        db_count = self.snapshot_info.record_count if self.snapshot_info else 0
        suggested = sum(1 for x in self.all_items if x.category == "建议修改")
        unchanged = sum(1 for x in self.all_items if x.category == "无需修改")
        review = sum(1 for x in self.all_items if x.category == CATEGORY_REVIEW)
        llm_review = sum(1 for x in self.all_items if x.category == CATEGORY_LLM_REVIEW)
        ai_reviewed_category = sum(1 for x in self.all_items if x.category == CATEGORY_AI_REVIEWED)
        confirmed_pending = sum(1 for x in self.all_items if x.category == CATEGORY_CONFIRMED)
        archive = sum(1 for x in self.all_items if ATTRIBUTE_ARCHIVE in (x.attributes or []))
        cosplay = sum(1 for x in self.all_items if ATTRIBUTE_COSPLAY in (x.attributes or []))
        manual_changed = sum(1 for x in self.all_items if x.has_manual_version or x.manual_edited)
        confirmed = sum(1 for x in self.all_items if x.manual_confirmed)
        values = {
            "数据库": db_count, "本批": total, "匹配": matched,
            "未匹配": unmatched, "异常": errors, "无需修改": unchanged,
            "建议修改": suggested, CATEGORY_LLM_REVIEW: llm_review,
            CATEGORY_AI_REVIEWED: ai_reviewed_category, "人工复核": review,
            "已确认": confirmed_pending, "画集": archive,
            "Cosplay": cosplay, "人工修改": manual_changed,
            "人工确认": confirmed,
            **{status: sum(x.ai_status == status for x in self.all_items) for status in AI_STATES},
        }
        for name, value in values.items():
            self.status_values[name].setText(str(value))
        self.summary_label.setText(", ".join(f"{name}：{value}" for name, value in values.items()))

    # ───────────────── 搜索 / 右键菜单 / 分析报告 ─────────────────
    def _focus_search(self):
        self.search_edit.setFocus(Qt.ShortcutFocusReason)
        self.search_edit.selectAll()

    def _clear_search_if_focused(self):
        if self.search_edit.hasFocus() and self.search_edit.text():
            self.search_edit.clear()

    def _insert_search_condition(self, condition: str):
        condition = str(condition or "").strip()
        if not condition:
            return
        current = self.search_edit.text().strip()
        tokens = current.split() if current else []
        if condition in AI_STATES:
            tokens = [token for token in tokens if token not in AI_STATES]
            current = " ".join(tokens)
        if condition not in tokens:
            self.search_edit.setText((current + " " + condition).strip())
        self.search_edit.setFocus(Qt.ShortcutFocusReason)
        self.search_edit.setCursorPosition(len(self.search_edit.text()))

    def _plain_search_fields(self, item: WorkItem) -> list[str]:
        record = item.record
        analysis = (item.extra or {}).get("analysis", {}) if isinstance(item.extra, dict) else {}
        analysis_texts: list[str] = []
        for key in ("warnings", "reasons", "flags", "artist_tags", "group_tags", "parody_tags", "language_tags", "other_tags", "needs_ai_reasons", "llm_review_reasons"):
            value = analysis.get(key)
            if isinstance(value, (list, tuple, set)):
                analysis_texts.extend(str(x) for x in value if x)
            elif value:
                analysis_texts.append(str(value))
        if item.needs_ai or analysis.get("needs_ai"):
            analysis_texts.extend(["needs_ai", "需要AI", "需要 ai", "AI复核"])
        else:
            analysis_texts.extend(["needs_ai=false", "无需AI", "无需 ai"])
        if item.manual_edited or item.has_manual_version:
            analysis_texts.extend(["人工修改", "手动修改", "手动版本"])
        if item.manual_confirmed:
            analysis_texts.extend(["人工确认", "已确认"])
        else:
            analysis_texts.extend(["未确认", "人工未确认"])
        analysis_texts.extend(item.attributes or [])
        return [
            record.title_jpn if record else "",
            record.title if record else "",
            record.url if record else "",
            record.category if record else "",
            record.tags_raw if record else "",
            item.category, item.match_method, item.warning, item.local_id,
            f"{item.original_name}{item.suffix}",
            f"{item.suggested_name}{item.suffix}",
            f"{item.program_suggested_name}{item.suffix}",
            f"{item.manual_name}{item.suffix}" if item.manual_name else "",
            *analysis_texts,
        ]

    def _item_matches_search(self, item: WorkItem, query: str) -> bool:
        if not query:
            return True
        record = item.record
        fields = self._plain_search_fields(item)

        # V0.2.26 仍只开放两个强需求结构化条件；其余保持原全文搜索。
        # 多个条件按 AND 组合，菜单选择后会直接写回搜索栏。
        try:
            tokens = shlex.split(query)
        except Exception:
            tokens = query.split()
        if not tokens:
            return True

        structured_seen = False
        plain_tokens: list[str] = []
        for token in tokens:
            lower = token.casefold()
            if token in AI_STATES:
                structured_seen = True
                if item.ai_status != token:
                    return False
            elif token in {"画廊关联冲突", "attribute:gallery_conflict"}:
                structured_seen = True
                if ATTRIBUTE_GALLERY_CONFLICT not in item.attributes:
                    return False
            elif lower.startswith("category:") or lower.startswith("cat:"):
                structured_seen = True
                expected = token.split(":", 1)[1].strip().casefold()
                actual = (record.category if record else "").strip().casefold()
                if expected and actual != expected:
                    return False
            elif lower in {"archive:true", "attribute:archive"}:
                structured_seen = True
                if ATTRIBUTE_ARCHIVE not in (item.attributes or []):
                    return False
            else:
                plain_tokens.append(token)

        if plain_tokens:
            blob = "\n".join(str(v) for v in fields if v).casefold()
            if structured_seen:
                return all(tok.casefold() in blob for tok in plain_tokens)
            # 保留 0.2.23 的普通全文搜索行为：整段文字作为一个查询。
            q = query.casefold()
            return any(q in str(value).casefold() for value in fields if value)
        return True

    def _on_search_text_changed(self, *_args):
        self.search_debounce_timer.start()

    def _reset_search_state(self, target_category: str | None = None):
        """Drop the temporary search view without restoring any search selection."""
        self.search_debounce_timer.stop()
        if hasattr(self, "search_edit"):
            self.search_edit.blockSignals(True)
            self.search_edit.clear()
            self.search_edit.blockSignals(False)
        self._search_query = ""
        self.search_results = []
        self.search_page = 1
        self._set_search_tab_active(False)
        target = target_category if target_category in CATEGORIES else self._last_ordinary_category
        if target not in CATEGORIES:
            target = CATEGORIES[0]
        self._suppress_tab_change = True
        try:
            self.tabs.setCurrentWidget(self.views[target])
        finally:
            self._suppress_tab_change = False
        self._active_scope_key = target
        self._last_ordinary_category = target
        self._pre_search_category = target

    def _apply_search_query(self):
        if self._batch_busy:
            return
        query = self.search_edit.text().strip()
        if query == self._search_query:
            return
        perf = self._perf_diag
        started = perf_counter() if perf else 0

        self._commit_active_edit()
        previous_scope = self.current_category()
        if previous_scope != SEARCH_RESULTS_LABEL:
            self._pre_search_category = previous_scope
            self._last_ordinary_category = previous_scope

        # 搜索词变化或进入/退出搜索都建立新的选择作用域。
        for item in self.all_items:
            item.checked = False

        self._search_query = query
        self.search_page = 1
        if query:
            self.search_results = sort_items(
                item for item in self.all_items if self._item_matches_search(item, query)
            )
            self._set_search_tab_active(True)
            self._refresh_scope_page(SEARCH_RESULTS_LABEL, scroll_top=True)
            self.refresh_tab_titles()
            self._suppress_tab_change = True
            try:
                self.tabs.setCurrentWidget(self.search_view)
            finally:
                self._suppress_tab_change = False
            self._active_scope_key = SEARCH_RESULTS_LABEL
        else:
            self.search_results = []
            target = self._pre_search_category if self._pre_search_category in CATEGORIES else CATEGORIES[0]
            self._set_search_tab_active(False)
            self._suppress_tab_change = True
            try:
                self.tabs.setCurrentWidget(self.views[target])
            finally:
                self._suppress_tab_change = False
            self._active_scope_key = target
            self._last_ordinary_category = target
            self._refresh_scope_page(target, scroll_top=True)
            self.refresh_tab_titles()

        self._clear_current_context(all_views=True)
        self.update_selected_count()
        self._schedule_session_save()
        if perf:
            perf.mark("search.apply", started, result_count=len(self.search_results))

    def _show_card_context_menu(self, view: CardListView, pos):
        index = view.indexAt(pos)
        if not index.isValid():
            return
        item = index.data(CardListModel.ITEM_ROLE)
        if not item:
            return
        from PySide6.QtWidgets import QMenu
        menu = QMenu(view)
        has_url = bool(item.record and item.record.url and item.record.url.strip())
        open_gallery = menu.addAction("打开画廊")
        open_gallery.setEnabled(has_url)
        open_gallery.triggered.connect(lambda: self._open_gallery_for_item(item))
        open_local = menu.addAction("打开本地位置")
        open_local.setEnabled(bool(item.original_path))
        open_local.triggered.connect(lambda: self._open_local_for_item(item))
        open_manga = menu.addAction("打开漫画")
        open_manga.setEnabled(bool(item.original_path))
        open_manga.triggered.connect(lambda: self._open_manga_for_item(item))
        copy_url = menu.addAction("复制画廊URL")
        copy_url.setEnabled(has_url)
        copy_url.triggered.connect(lambda: self._copy_url_for_item(item))
        menu.exec(view.viewport().mapToGlobal(pos))

    def _report_text_for_item(self, item: WorkItem, ordinal: int | None = None) -> str:
        record = item.record
        analysis = (item.extra or {}).get("analysis") if isinstance(item.extra, dict) else None
        lines: list[str] = []
        if ordinal is not None:
            lines.append(f"==================== #{ordinal} ====================")
        lines.append(f"local_id：{item.local_id}")
        lines.append(f"文件名：{item.original_name}{item.suffix}")
        lines.append(f"处理建议：{item.category}")
        lines.append(f"程序分类：{item.program_category or '-'}")
        lines.append(f"人工标记：{item.manual_category_value if item.manual_category else '(无)'}")
        lines.append("")
        if isinstance(analysis, dict):
            lines += [
                "──────── 命名分析 ────────",
                f"标题候选来源：{analysis.get('metadata_candidate_source', '-')}",
                f"标题候选：{analysis.get('metadata_candidate') or '(空)'}",
                f"候选可信度：{'可自动采用' if analysis.get('metadata_candidate_reliable') else '需要人工确认'}",
                f"与当前名称相似度：{float(analysis.get('metadata_similarity') or 0):.1%}",
                f"中文翻译状态：{'中文翻译版' if analysis.get('is_chinese_translation') else ('中文原始语言' if analysis.get('chinese_original') else '否/不确定')}",
                f"作者tags：{', '.join(analysis.get('artist_tags') or []) or '(无)'}",
                f"团体tags：{', '.join(analysis.get('group_tags') or []) or '(无)'}",
                f"语言tags：{', '.join(analysis.get('language_tags') or []) or '(无)'}",
            ]
            if analysis.get('has_no_text_language'):
                lines.append("无文字状态：是（最终名称应包含 [No Text]）")
            if analysis.get('gallery_match_risk'):
                lines.append("画廊关联风险：存疑（本地关键 metadata 与 E-H tags 不一致）")
            if analysis.get('title_fields_swapped_suspected'):
                lines.append("字段疑似反置：是")
            if analysis.get('leading_parenthesis'):
                lines.append(f"开头圆括号识别：{analysis.get('leading_parenthesis')} / {analysis.get('leading_parenthesis_type') or 'NONE'}")
            if analysis.get('rough_translation'):
                lines.append("rough translation：是")
            if analysis.get('ai_generated'):
                lines.append("ai generated：是")
            if analysis.get('extraneous_ads'):
                lines.append("extraneous ads：是")
            if analysis.get('page_diff') is not None and (analysis.get('page_diff') or analysis.get('page_diff_level') not in (None, '正常范围')):
                ratio = analysis.get('page_diff_ratio')
                ratio_text = f"{float(ratio):.1%}" if isinstance(ratio, (int, float)) else "-"
                lines.append(f"页数差：{analysis.get('page_diff')}（{ratio_text}，{analysis.get('page_diff_level') or '-'}）")
            if analysis.get('composite_manga_dir'):
                lines.append(
                    f"复合漫画目录：直接图片 {analysis.get('direct_image_count') or 0}，"
                    f"子目录 {analysis.get('nested_dir_count') or 0}，子目录图片 {analysis.get('nested_image_count') or 0}，"
                    f"本地统计页数 {analysis.get('effective_local_page_count') or 0}"
                )
            lines.append(f"needs_ai：{'是' if analysis.get('needs_ai') else '否'}")
            field_sources = analysis.get('field_sources') or {}
            if isinstance(field_sources, dict) and field_sources:
                lines.append("字段来源：")
                for key, value in field_sources.items():
                    lines.append(f"  {key}：{value}")
            reasons = analysis.get('reasons') or []
            if reasons:
                lines += ["", "自动判断依据："] + [f"  • {x}" for x in reasons]
            warnings = analysis.get('warnings') or []
            if warnings:
                lines += ["", "分析警告："] + [f"  • {x}" for x in warnings]
        if record:
            try:
                tags = json.loads(record.tags_raw or "{}")
                if not isinstance(tags, dict):
                    tags = {}
            except Exception:
                tags = {}
            lines += [
                "", "──────── 数据库信息 ────────",
                f"标题：{record.title_jpn or '(空)'}",
                f"英文标题：{record.title or '(空)'}",
                f"画廊URL：{record.url or '(空)'}",
                "", "基础信息：",
                f"  category：{record.category or '(空)'}",
                f"  status：{record.status}",
                f"  rating：{record.rating}",
                f"  filecount：{record.filecount}",
                f"  pageCount：{record.page_count}",
                "", "Tags：",
            ]
            if tags:
                preferred = ["language", "parody", "character", "group", "artist", "male", "female", "mixed", "other", "reclass"]
                keys = [k for k in preferred if k in tags] + [k for k in tags if k not in preferred]
                for key in keys:
                    values = tags.get(key)
                    if isinstance(values, str):
                        values = [values]
                    if isinstance(values, list):
                        clean = [str(v).strip() for v in values if str(v).strip()]
                        if clean:
                            lines.append(f"  {key}:\t{', '.join(clean)}")
            else:
                lines.append("  (无)")
            lines += ["", f"数据库 filepath：{record.filepath}"]
        manual_text = item.manual_name if item.manual_name else "(尚无手动版本)"
        source_text = "手动版本" if item.name_source == "manual" else "程序建议"
        lines += [
            "", "──────── 本地 / 编辑状态 ────────",
            f"处理建议：{item.category}",
            f"程序分类：{item.program_category or '-'}",
            f"人工标记：{item.manual_category_value if item.manual_category else '(无)'}",
            f"作品属性：{', '.join(item.attributes) if item.attributes else '(无)'}",
            f"人工修改：{'是' if (item.manual_edited or item.has_manual_version) else '否'}",
            f"人工确认：{'是' if item.manual_confirmed else '否'}",
            f"确认名称：{item.confirmed_name if item.manual_confirmed else '(未确认)'}",
            f"AI审查：{item.ai_status}",
            f"AI模型：{item.ai_model or '-'}",
            f"AI审查次数：{item.ai_review_count}",
            f"程序建议：{item.program_suggested_name}{item.suffix}",
            f"手动版本：{manual_text}{item.suffix if item.manual_name else ''}",
            f"当前使用：{source_text}",
            f"匹配方式：{item.match_method or '-'}",
            f"相对路径：{item.extra.get('relative_path') or '(根目录直接项目)'}",
            f"当前实际路径：{item.original_path}",
            f"文件操作状态：{item.file_status}",
        ]
        return "\n".join(lines)

    def export_analysis_report(self):
        if not self.all_items:
            QMessageBox.information(self, "没有可导出的内容", "请先建立数据库快照并扫描本批。")
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("导出分析报告")
        dialog.resize(470, 300)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("选择导出范围："))

        checked_count = len(self.selected_items_current_scope())
        rb_checked = QRadioButton(f"已勾选漫画（{self.current_category()}，{checked_count} 本）")
        rb_categories = QRadioButton("指定分类")
        rb_all = QRadioButton(f"全部漫画（{len(self.all_items)} 本）")
        rb_checked.setChecked(bool(checked_count))
        if not checked_count:
            rb_categories.setChecked(True)
        layout.addWidget(rb_checked)
        category_row = QHBoxLayout()
        category_row.addWidget(rb_categories)
        choose_categories_btn = QPushButton("选择分类")
        selected_categories_label = QLabel()
        category_row.addWidget(choose_categories_btn)
        category_row.addWidget(selected_categories_label)
        category_row.addStretch(1)
        layout.addLayout(category_row)
        layout.addWidget(rb_all)
        initial_scope = self.current_category()
        chosen_categories: set[str] = {
            initial_scope if initial_scope in CATEGORIES or initial_scope == SEARCH_RESULTS_LABEL else CATEGORIES[0]
        }

        def update_chosen_label():
            selected_categories_label.setText(f"已选择 {len(chosen_categories)} 个分类")

        def choose_categories():
            picker = QDialog(dialog)
            picker.setWindowTitle("选择导出分类")
            picker.resize(380, 460)
            picker_layout = QVBoxLayout(picker)
            category_list = QListWidget()
            ordered = list(CATEGORIES) + [SEARCH_RESULTS_LABEL]
            for category in ordered:
                count = len(self.search_results) if category == SEARCH_RESULTS_LABEL else len(self.category_items.get(category, []))
                row = QListWidgetItem(f"{category}（{count}）")
                row.setData(Qt.UserRole, category)
                row.setFlags(row.flags() | Qt.ItemIsUserCheckable)
                row.setCheckState(Qt.Checked if category in chosen_categories else Qt.Unchecked)
                category_list.addItem(row)
            picker_layout.addWidget(category_list, 1)

            tools = QHBoxLayout()
            all_btn = QPushButton("全选")
            invert_btn = QPushButton("反选")
            clear_btn = QPushButton("清空")
            all_btn.clicked.connect(
                lambda: [category_list.item(i).setCheckState(Qt.Checked) for i in range(category_list.count())]
            )
            invert_btn.clicked.connect(
                lambda: [
                    category_list.item(i).setCheckState(
                        Qt.Unchecked if category_list.item(i).checkState() == Qt.Checked else Qt.Checked
                    )
                    for i in range(category_list.count())
                ]
            )
            clear_btn.clicked.connect(
                lambda: [category_list.item(i).setCheckState(Qt.Unchecked) for i in range(category_list.count())]
            )
            tools.addWidget(all_btn)
            tools.addWidget(invert_btn)
            tools.addWidget(clear_btn)
            picker_layout.addLayout(tools)
            picker_buttons = QDialogButtonBox(
                QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            )
            picker_buttons.accepted.connect(picker.accept)
            picker_buttons.rejected.connect(picker.reject)
            picker_layout.addWidget(picker_buttons)
            if picker.exec() == QDialog.Accepted:
                chosen_categories.clear()
                for i in range(category_list.count()):
                    row = category_list.item(i)
                    if row.checkState() == Qt.Checked:
                        chosen_categories.add(str(row.data(Qt.UserRole)))
                update_chosen_label()

        choose_categories_btn.clicked.connect(choose_categories)
        update_chosen_label()

        def sync_category_enabled():
            choose_categories_btn.setEnabled(rb_categories.isChecked())
            selected_categories_label.setEnabled(rb_categories.isChecked())
        rb_checked.toggled.connect(sync_category_enabled)
        rb_categories.toggled.connect(sync_category_enabled)
        rb_all.toggled.connect(sync_category_enabled)
        sync_category_enabled()

        hint = QLabel("TXT 会包含每本漫画的命名分析、数据库信息、Tags、本地/编辑状态，便于直接交给 AI 批量复核。")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("导出")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return

        if rb_checked.isChecked():
            items = list(self.selected_items_current_scope())
            if not items:
                QMessageBox.information(self, "没有勾选漫画", "当前作用域没有勾选任何漫画。")
                return
            scope_name = f"已勾选_{self.current_category()}"
        elif rb_categories.isChecked():
            if not chosen_categories:
                QMessageBox.information(self, "没有选择分类", "请至少选择一个分类。")
                return
            items = ordered_categories_export(
                self.category_items,
                chosen_categories,
                search_label=SEARCH_RESULTS_LABEL,
                search_results=self.search_results,
            )
            ordered_names = [c for c in CATEGORIES if c in chosen_categories]
            if SEARCH_RESULTS_LABEL in chosen_categories:
                ordered_names.append(SEARCH_RESULTS_LABEL)
            scope_name = "分类_" + "_".join(ordered_names)
        else:
            items = ordered_all_export(self.category_items)
            scope_name = "全部"

        # 最终点击“导出”后立即固定成员和顺序；后续界面变化不再参与本次导出。
        item_snapshot = tuple(items)
        local_id_snapshot = tuple(item.local_id for item in item_snapshot)

        default_dir = self.work_edit.text().strip() or str(Path.home())
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_scope = re.sub(r'[\\/:*?"<>|]+', '_', scope_name)
        default_path = str(Path(default_dir) / f"命名分析报告_{safe_scope}_{stamp}.txt")
        path, _ = QFileDialog.getSaveFileName(self, "导出分析报告", default_path, "文本文件 (*.txt);;所有文件 (*)")
        if not path:
            return
        try:
            header = [
                f"漫画批量改名工具 V{VERSION} - 命名分析报告",
                f"导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
                f"导出范围：{scope_name}",
                f"漫画数量：{len(local_id_snapshot)}",
                "",
            ]
            body = "\n\n".join(
                self._report_text_for_item(item, i) for i, item in enumerate(item_snapshot, 1)
            )
            Path(path).write_text("\n".join(header) + body + "\n", encoding="utf-8")
        except Exception as exc:
            QMessageBox.warning(self, "导出失败", f"无法写入分析报告：\n\n{exc}")
            return
        self._show_status_message(f"已导出 {len(item_snapshot)} 本漫画的分析报告。", 5000)
        QMessageBox.information(self, "导出完成", f"已导出 {len(item_snapshot)} 本漫画：\n{path}")

    # ───────────────── 当前标签 / 勾选 ─────────────────
    def _on_search_results_tab(self) -> bool:
        return bool(self.search_tab_active and self.search_view is not None and self.tabs.currentWidget() is self.search_view)

    def current_category(self) -> str:
        if self._on_search_results_tab():
            return SEARCH_RESULTS_LABEL
        widget = self.tabs.currentWidget()
        for category, view in self.views.items():
            if view is widget:
                return category
        return CATEGORIES[0]

    def current_view(self) -> CardListView:
        if self._on_search_results_tab() and self.search_view is not None:
            return self.search_view
        return self.views[self.current_category()]

    def current_model(self) -> CardListModel:
        if self._on_search_results_tab() and self.search_model is not None:
            return self.search_model
        return self.models[self.current_category()]

    def selected_rows(self) -> list[int]:
        return self.current_model().checked_rows()

    def selected_items_current_scope(self) -> list[WorkItem]:
        return [item for item in self._scope_items() if item.checked]

    def select_all_current(self):
        for item in self._scope_items():
            item.checked = True
        self.current_model().refresh_checks()
        self.update_selected_count()

    def clear_current_selection(self):
        for item in self._scope_items():
            item.checked = False
        self.current_model().refresh_checks()
        self.update_selected_count()

    def invert_current(self):
        for item in self._scope_items():
            item.checked = not item.checked
        self.current_model().refresh_checks()
        self.update_selected_count()

    def clear_all_selections(self):
        for item in self.all_items:
            item.checked = False
        for model in self.models.values():
            model.refresh_checks()
        if self.search_model is not None:
            self.search_model.refresh_checks()
        self.update_selected_count()

    def on_tab_changed(self, _index):
        if self._suppress_tab_change:
            return
        perf = self._perf_diag
        started = perf_counter() if perf else 0
        stage = started
        self._commit_all_edits()
        if perf:
            stage = perf.mark("tab.commit_edits", stage)
        previous_scope = self._active_scope_key
        new_scope = self.current_category()
        old_count = 0
        if previous_scope in CATEGORIES or previous_scope == SEARCH_RESULTS_LABEL:
            previous_items = self._scope_items(previous_scope)
            old_count = len(previous_items)
            for item in previous_items:
                item.checked = False
            if perf:
                stage = perf.mark("tab.clear_checks", stage, old_count=old_count)
            try:
                self._model_for_scope(previous_scope).refresh_checks()
            except Exception:
                pass
            if perf:
                stage = perf.mark("tab.refresh_checks", stage)
        self._active_scope_key = new_scope
        if new_scope in CATEGORIES:
            self._last_ordinary_category = new_scope
            if not self._search_query:
                self._pre_search_category = new_scope
        self._clear_current_context(all_views=True)
        if perf:
            stage = perf.mark("tab.clear_current_context", stage)
        self.update_selected_count()
        if perf:
            stage = perf.mark("tab.update_selection", stage, new_count=len(self._scope_items(new_scope)))
        self._schedule_session_save()
        if perf:
            perf.mark("tab.total", started, old_count=old_count, new_count=len(self._scope_items(new_scope)))
            QTimer.singleShot(0, lambda p=perf, t=started: p.mark("tab.next_event_turn", t))

    def update_selected_count(self, *args):
        perf = getattr(self, "_perf_diag", None)
        started = perf_counter() if perf else 0
        count = sum(1 for item in self._scope_items() if item.checked)
        self.selected_label.setText(f"已勾选：{count}")
        self._update_pager()
        self._invalidate_execution_validation()
        if perf:
            perf.mark("selection.update", started, checked=count)

    def _checked_items_or_notice(self, action_name: str) -> list[WorkItem]:
        items = self.selected_items_current_scope()
        if not items:
            QMessageBox.information(self, "没有勾选漫画", f"请先勾选要{action_name}的漫画。")
            return []
        return items

    def _snapshot_checked_ids(self, action_name: str) -> tuple[str, ...]:
        items = self._checked_items_or_notice(action_name)
        return tuple(item.local_id for item in items)

    def _items_for_local_ids(self, local_ids) -> list[WorkItem]:
        by_id = {item.local_id: item for item in self.all_items}
        return [by_id[local_id] for local_id in local_ids if local_id in by_id]

    def _set_batch_busy(self, busy: bool):
        self._batch_busy = bool(busy)
        enabled = not self._batch_busy
        for widget in (
            self.tabs,
            self.search_edit,
            self.search_filter_btn,
            self.pager_widget,
            self.batch_content,
            self.destination_content,
            self.scan_btn,
            self.reanalyze_action,
        ):
            widget.setEnabled(enabled)
        if enabled:
            self.reanalyze_action.setEnabled(bool(self.all_items))
            self._update_undo_buttons()
        self._update_pager()

    @staticmethod
    def _classification_undo_state(item: WorkItem) -> dict:
        return snapshot_classification_state(item)

    @staticmethod
    def _attribute_undo_state(item: WorkItem, attribute: str) -> dict:
        return snapshot_attribute_state(item, attribute)

    def _snapshot_undo_state(self, item: WorkItem, kind: str, attribute: str = "") -> dict:
        if kind == "classification":
            return self._classification_undo_state(item)
        if kind == "attribute":
            return self._attribute_undo_state(item, attribute)
        raise ValueError(f"unknown undo kind: {kind}")

    @staticmethod
    def _apply_classification_undo_state(item: WorkItem, state: dict):
        apply_classification_state(item, state)

    @staticmethod
    def _apply_attribute_undo_state(item: WorkItem, attribute: str, state: dict):
        apply_attribute_state(item, attribute, state)

    def _apply_undo_state(self, item: WorkItem, record: UndoRecord, state: dict):
        if record.kind == "classification":
            self._apply_classification_undo_state(item, state)
        elif record.kind == "attribute":
            self._apply_attribute_undo_state(item, record.attribute, state)
        else:
            raise ValueError(f"unknown undo kind: {record.kind}")

    def _push_undo_record(
        self,
        action_name: str,
        kind: str,
        before_states: dict[str, dict],
        items: list[WorkItem],
        *,
        attribute: str = "",
    ):
        changes: list[UndoChange] = []
        for item in items:
            before = before_states.get(item.local_id)
            if before is None:
                continue
            after = self._snapshot_undo_state(item, kind, attribute)
            if before != after:
                changes.append(UndoChange(item.local_id, before, after))
        if changes:
            self._undo_history.push(UndoRecord(action_name, kind, tuple(changes), attribute))
            self._update_undo_buttons()

    def _update_undo_buttons(self):
        if not hasattr(self, "undo_btn") or not hasattr(self, "redo_btn"):
            return
        undo_record = self._undo_history.next_undo
        redo_record = self._undo_history.next_redo
        self.undo_btn.setEnabled(undo_record is not None)
        self.redo_btn.setEnabled(redo_record is not None)
        self.undo_btn.setToolTip(
            f"撤销：{undo_record.action_name}（{undo_record.item_count} 项）"
            if undo_record else "当前没有可撤销的状态操作"
        )
        self.redo_btn.setToolTip(
            f"重做：{redo_record.action_name}（{redo_record.item_count} 项）"
            if redo_record else "当前没有可重做的状态操作"
        )

    def _clear_undo_history(self):
        self._undo_history.clear()
        self._update_undo_buttons()

    def _apply_history_record(self, record: UndoRecord, *, use_after: bool) -> int:
        self._commit_active_edit()
        anchor = self._capture_page_anchor()
        by_id = {item.local_id: item for item in self.all_items}
        applied = 0
        self._set_batch_busy(True)
        try:
            for change in record.changes:
                item = by_id.get(change.local_id)
                if item is None:
                    continue
                state = change.after if use_after else change.before
                self._apply_undo_state(item, record, state)
                applied += 1
            self.populate_categories(anchor=anchor, preserve_current=True)
            self.show_scan_summary()
            self._schedule_session_save()
            self._invalidate_execution_validation()
        finally:
            self._set_batch_busy(False)
        return applied

    def undo_last_state_action(self):
        record = self._undo_history.pop_undo()
        if record is None:
            self._update_undo_buttons()
            return
        applied = self._apply_history_record(record, use_after=False)
        self._undo_history.finish_undo(record)
        self._update_undo_buttons()
        self._show_status_message(f"已撤销：{record.action_name}（{applied} 项）", 5000)

    def redo_last_state_action(self):
        record = self._undo_history.pop_redo()
        if record is None:
            self._update_undo_buttons()
            return
        applied = self._apply_history_record(record, use_after=True)
        self._undo_history.finish_redo(record)
        self._update_undo_buttons()
        self._show_status_message(f"已重做：{record.action_name}（{applied} 项）", 5000)

    def _apply_batch_mutation(
        self,
        action_name: str,
        local_ids: tuple[str, ...],
        mutator,
        *,
        after_success=None,
        undo_kind: str | None = None,
        undo_attribute: str = "",
    ) -> tuple[list[WorkItem], list[tuple[str, str]], list[str]]:
        """Run one local_id snapshot as one refresh and, when requested, one undo unit."""
        if not local_ids:
            return [], [], []
        self._commit_active_edit()
        anchor = self._capture_page_anchor()
        successes: list[WorkItem] = []
        failures: list[tuple[str, str]] = []
        skipped: list[str] = []
        before_states: dict[str, dict] = {}
        self._set_batch_busy(True)
        try:
            by_id = {item.local_id: item for item in self.all_items}
            for local_id in local_ids:
                item = by_id.get(local_id)
                if item is None:
                    skipped.append(local_id)
                    continue
                try:
                    if undo_kind:
                        before_states[local_id] = self._snapshot_undo_state(
                            item, undo_kind, undo_attribute
                        )
                    applied = mutator(item)
                    if applied is False:
                        before_states.pop(local_id, None)
                        skipped.append(local_id)
                        continue
                    item.checked = False
                    successes.append(item)
                except Exception as exc:
                    item.checked = True
                    failures.append((local_id, str(exc)))
            if after_success is not None and successes:
                after_success(successes)
            if undo_kind and successes:
                self._push_undo_record(
                    action_name,
                    undo_kind,
                    before_states,
                    successes,
                    attribute=undo_attribute,
                )
            self.populate_categories(anchor=anchor, preserve_current=True)
            self.show_scan_summary()
            self._schedule_session_save()
            self._invalidate_execution_validation()
        finally:
            self._set_batch_busy(False)

        message = f"{action_name}完成：成功 {len(successes)}，失败 {len(failures)}，跳过 {len(skipped)}。"
        self._show_status_message(message, 6000)
        if failures:
            preview = "\n".join(f"{local_id}：{reason}" for local_id, reason in failures[:8])
            QMessageBox.warning(self, f"{action_name}部分失败", message + "\n\n" + preview)
        return successes, failures, skipped

    def move_selected_to(self, target_category: str, manual: bool):
        if target_category not in {
            CATEGORY_UNCHANGED, CATEGORY_SUGGESTED, CATEGORY_LLM_REVIEW,
            CATEGORY_REVIEW, CATEGORY_CONFIRMED,
        }:
            return
        local_ids = self._snapshot_checked_ids("处理")
        if not local_ids:
            return
        selected = self._items_for_local_ids(local_ids)
        if all(item.manual_category and item.manual_category_value == target_category for item in selected):
            self._show_status_message(f"选中项目已经全部标记为“{target_category}”。", 3500)
            return

        def mutate(item: WorkItem):
            self._mark_item(item, target_category)
            return True

        self._apply_batch_mutation(
            f"标记为“{target_category}”", local_ids, mutate, undo_kind="classification"
        )

    @staticmethod
    def _mark_item(item: WorkItem, category: str):
        if not item.program_category and not item.manual_category:
            item.program_category = item.category
        if item.manual_confirmed and category != CATEGORY_CONFIRMED:
            if item.suggested_name != item.program_suggested_name and not item.manual_name:
                item.manual_name = item.suggested_name
                item.name_source = "manual"
                item.manual_edited = True
        if category == CATEGORY_LLM_REVIEW:
            if item.manual_category and item.manual_category_value not in {CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED}:
                item.prior_manual_category = item.manual_category_value
            item.manual_ai_flow = "pending"
            item.ai_special_flow = True
        elif category != CATEGORY_AI_REVIEWED:
            item.manual_ai_flow = ""
        item.manual_category = True
        item.manual_category_value = category
        item.category = category
        item.manual_confirmed = category == CATEGORY_CONFIRMED
        item.confirmed_name = (item.suggested_name or item.original_name) if item.manual_confirmed else ""

    def clear_selected_marks(self):
        local_ids = self._snapshot_checked_ids("解除标记")
        if not local_ids:
            return

        def mutate(item: WorkItem):
            if not item.manual_category and not item.manual_confirmed:
                return False
            if item.manual_confirmed and item.suggested_name != item.program_suggested_name and not item.manual_name:
                item.manual_name = item.suggested_name
                item.name_source = "manual"
                item.manual_edited = True
            item.manual_category = False
            item.manual_category_value = ""
            item.manual_confirmed = False
            item.confirmed_name = ""
            item.category = item.program_category or item.category
            return True

        def reanalyze(successes):
            analyze_items(successes, preserve_manual_category=True, options=self._analysis_options())

        self._apply_batch_mutation(
            "解除标记",
            local_ids,
            mutate,
            after_success=reanalyze,
            undo_kind="classification",
        )

    def toggle_attribute_selected(self, attribute: str):
        local_ids = self._snapshot_checked_ids("切换属性")
        if not local_ids:
            return
        items = self._items_for_local_ids(local_ids)
        should_add = not all(attribute in (item.attributes or []) for item in items)

        def mutate(item: WorkItem):
            attrs = list(item.attributes or [])
            if should_add and attribute not in attrs:
                attrs.append(attribute)
            elif not should_add and attribute in attrs:
                attrs.remove(attribute)
            item.attributes = attrs
            item.manual_attribute_overrides = dict(item.manual_attribute_overrides or {})
            item.manual_attribute_overrides[attribute] = should_add
            return True

        action = "添加" if should_add else "移除"
        self._apply_batch_mutation(
            f"{action}“{attribute}”属性",
            local_ids,
            mutate,
            undo_kind="attribute",
            undo_attribute=attribute,
        )

    def confirm_selected_names(self):
        self.move_selected_to(CATEGORY_CONFIRMED, manual=True)

    def unconfirm_selected_names(self):
        self.clear_selected_marks()

    def set_selected_ai_reviewed(self, reviewed: bool):
        raise RuntimeError("手工标记 AI 已审已停用；请运行 API 审核")

    # ───────────────── 详情 / 名称版本切换 ─────────────────
    def _update_name_version_button(self, item: WorkItem | None):
        if item is None or not item.has_manual_version:
            self.name_version_btn.setEnabled(False)
            self.name_version_btn.setText("尚无手动版本")
            return
        self.name_version_btn.setEnabled(True)
        if item.name_source == "manual":
            self.name_version_btn.setText("切换到程序建议（保留手动版本）")
        else:
            self.name_version_btn.setText("返回手动版本")

    def _confirm_confirmed_edit(self, item: WorkItem, proposed_name: str) -> bool:
        if item.category != CATEGORY_CONFIRMED or proposed_name == item.suggested_name:
            return True
        box = QMessageBox(self)
        box.setWindowTitle("确认修改")
        box.setText("该条目当前为“已确认”分类，是否确认修改？")
        accept = box.addButton("确认", QMessageBox.ButtonRole.AcceptRole)
        cancel = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(accept)
        box.exec()
        return box.clickedButton() is accept

    def _current_item(self) -> WorkItem | None:
        index = self.current_view().currentIndex()
        if not index.isValid():
            return None
        return index.data(CardListModel.ITEM_ROLE)

    def _update_detail_action_buttons(self, item: WorkItem | None):
        has_item = item is not None
        has_url = bool(item and item.record and item.record.url)
        self.open_gallery_btn.setEnabled(has_url)
        self.copy_url_btn.setEnabled(has_url)
        has_path = bool(has_item and item.original_path)
        self.open_local_btn.setEnabled(has_path)
        self.open_manga_btn.setEnabled(has_path)

    def _open_gallery_for_item(self, item: WorkItem | None):
        url = item.record.url.strip() if item and item.record and item.record.url else ""
        if not url:
            QMessageBox.information(self, "没有画廊链接", "当前项目没有可用的 E-H/ExHentai URL。")
            return
        mode = str(self.settings_data.get("gallery_browser", "default"))
        if mode == "firefox":
            firefox = str(self.settings_data.get("firefox_path", "")).strip() or self._find_firefox()
            if firefox and os.path.isfile(firefox):
                try:
                    subprocess.Popen([firefox, url])
                    return
                except Exception as exc:
                    QMessageBox.warning(self, "Firefox 打开失败", f"无法启动 Firefox：\n\n{exc}\n\n将改用系统默认浏览器。")
            else:
                QMessageBox.information(self, "未找到 Firefox", "设置中选择了 Firefox，但没有找到 firefox.exe。将改用系统默认浏览器。")
        webbrowser.open(url)

    def open_current_gallery(self):
        self._open_gallery_for_item(self._current_item())

    def _copy_url_for_item(self, item: WorkItem | None):
        url = item.record.url.strip() if item and item.record and item.record.url else ""
        if url:
            QApplication.clipboard().setText(url)
            self._show_status_message("画廊URL 已复制。", 2500)

    def copy_current_url(self):
        self._copy_url_for_item(self._current_item())

    def _open_local_for_item(self, item: WorkItem | None):
        """打开漫画所在的上一级目录，并选中漫画本体。

        文件夹型与压缩包型行为保持一致：都停留在父目录，方便人工查看同层其他漫画。
        """
        if not item or not item.original_path:
            return
        path = os.path.abspath(item.original_path)
        parent = os.path.dirname(path)
        try:
            if os.name == "nt":
                if os.path.exists(path):
                    subprocess.Popen(["explorer", "/select,", path])
                elif os.path.isdir(parent):
                    os.startfile(parent)
                else:
                    raise FileNotFoundError(path)
            else:
                target = parent if parent else path
                subprocess.Popen(["xdg-open", target])
        except Exception as exc:
            QMessageBox.warning(self, "无法打开本地位置", f"无法打开：\n{path}\n\n{exc}")

    def open_current_local(self):
        self._open_local_for_item(self._current_item())

    def _open_manga_for_item(self, item: WorkItem | None):
        """直接打开漫画本体：文件夹进入图片目录，压缩包交给默认程序。"""
        if not item or not item.original_path:
            return
        path = os.path.abspath(item.original_path)
        try:
            if os.name == "nt":
                if os.path.exists(path):
                    os.startfile(path)
                else:
                    raise FileNotFoundError(path)
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception as exc:
            QMessageBox.warning(self, "无法打开漫画", f"无法打开：\n{path}\n\n{exc}")

    def open_current_manga(self):
        self._open_manga_for_item(self._current_item())

    def _append_wrapped_tag_line(self, lines, key, values, tab_stop):
        """按实际像素宽度预换行 Tags，并用同一个制表位实现可靠悬挂缩进。

        不能用若干普通空格模拟缩进：详情面板使用比例字体，英文 namespace、
        空格和正文字符宽度不同，自动换行后会明显错位。这里让首行与续行
        都落到同一个 tab stop，因此无论字体/DPI 如何，续行都会从 tag 值起点开始。
        """
        fm = QFontMetrics(self.details.font())
        viewport_width = max(180, self.details.viewport().width() - 24)
        value_width = max(80, viewport_width - int(tab_stop) - 8)

        chunks = [str(v).strip() for v in values if str(v).strip()]
        if not chunks:
            return

        wrapped = []
        current = ""
        for value in chunks:
            piece = value if not current else ", " + value
            if current and fm.horizontalAdvance(current + piece) > value_width:
                wrapped.append(current)
                current = value
            else:
                current += piece
        if current:
            wrapped.append(current)

        # 首行 namespace 后与续行都使用同一制表位，形成真正的悬挂缩进。
        lines.append(f"  {key}:\t{wrapped[0]}")
        for part in wrapped[1:]:
            lines.append(f"\t{part}")

    def show_item_details(self, item: WorkItem):
        if not item:
            self.details.clear()
            self._update_name_version_button(None)
            self._update_confirmation_button(None)
            self._update_detail_action_buttons(None)
            return

        self._update_detail_action_buttons(item)
        record = item.record
        lines: list[str] = []

        # V0.2.9：详情先给人工复核真正关心的“命名分析”；
        # 原始数据库字段下移到数据库信息区，避免与主卡片前三行重复抢位置。
        analysis = (item.extra or {}).get("analysis") if isinstance(item.extra, dict) else None
        if isinstance(analysis, dict):
            lines += [
                "──────── 命名分析 ────────",
                f"标题候选来源：{analysis.get('metadata_candidate_source', '-')}",
                f"标题候选：{analysis.get('metadata_candidate') or '(空)'}",
                f"候选可信度：{'可自动采用' if analysis.get('metadata_candidate_reliable') else '需要人工确认'}",
                f"与当前名称相似度：{float(analysis.get('metadata_similarity') or 0):.1%}",
                f"中文翻译状态：{'中文翻译版' if analysis.get('is_chinese_translation') else ('中文原始语言' if analysis.get('chinese_original') else '否/不确定')}",
                f"作者tags：{', '.join(analysis.get('artist_tags') or []) or '(无)'}",
                f"团体tags：{', '.join(analysis.get('group_tags') or []) or '(无)'}",
                f"语言tags：{', '.join(analysis.get('language_tags') or []) or '(无)'}",
            ]
            if analysis.get('has_no_text_language'):
                lines.append("无文字状态：是（最终名称应包含 [No Text]）")
            if analysis.get('gallery_match_risk'):
                lines.append("画廊关联风险：存疑（本地关键 metadata 与 E-H tags 不一致）")

            # 技术判断仍保留，但分析器版本不再占据人工复核区。
            if analysis.get('title_fields_swapped_suspected'):
                lines.append("字段疑似反置：是")
            if analysis.get('leading_parenthesis'):
                lines.append(
                    f"开头圆括号识别：{analysis.get('leading_parenthesis')} / {analysis.get('leading_parenthesis_type') or 'NONE'}"
                )
            if analysis.get('rough_translation'):
                lines.append("rough translation：是")
            if analysis.get('ai_generated'):
                lines.append("ai generated：是")
            if analysis.get('extraneous_ads'):
                lines.append("extraneous ads：是")

            if analysis.get("page_diff") is not None:
                ratio = analysis.get("page_diff_ratio")
                ratio_text = f"{float(ratio):.1%}" if isinstance(ratio, (int, float)) else "-"
                if analysis.get("page_diff") or analysis.get("page_diff_level") not in (None, "正常范围"):
                    lines.append(
                        f"页数差：{analysis.get('page_diff')}（{ratio_text}，{analysis.get('page_diff_level') or '-'}）"
                    )
            if analysis.get("composite_manga_dir"):
                lines.append(
                    f"复合漫画目录：直接图片 {analysis.get('direct_image_count') or 0}，"
                    f"子目录 {analysis.get('nested_dir_count') or 0}，子目录图片 {analysis.get('nested_image_count') or 0}，"
                    f"本地统计页数 {analysis.get('effective_local_page_count') or 0}"
                )
            lines.append(f"needs_ai：{'是' if analysis.get('needs_ai') else '否'}")
            field_sources = analysis.get('field_sources') or {}
            if isinstance(field_sources, dict) and field_sources:
                lines.append("字段来源：")
                for key, value in field_sources.items():
                    lines.append(f"  {key}：{value}")

            reasons = analysis.get("reasons") or []
            if reasons:
                lines += ["", "自动判断依据："] + [f"  • {x}" for x in reasons]
            warnings = analysis.get("warnings") or []
            if warnings:
                lines += ["", "分析警告："] + [f"  • {x}" for x in warnings]
        if record:
            try:
                tags = json.loads(record.tags_raw or "{}")
                if not isinstance(tags, dict):
                    tags = {}
            except Exception:
                tags = {}

            lines += [
                "",
                "──────── 数据库信息 ────────",
                f"标题：{record.title_jpn or '(空)'}",
                f"英文标题：{record.title or '(空)'}",
                f"画廊URL：{record.url or '(空)'}",
                "",
                "基础信息：",
                f"  category：{record.category or '(空)'}",
                f"  status：{record.status}",
                f"  rating：{record.rating}",
                f"  filecount：{record.filecount}",
                f"  pageCount：{record.page_count}",
                "",
                "Tags：",
            ]

            # 参考 exhentai-manga-manager 的分类展示方式：左侧 namespace，
            # 右侧同类 tag 以逗号连接。只展示实际存在的 namespace。
            if tags:
                preferred_order = [
                    "language", "parody", "character", "group", "artist",
                    "male", "female", "mixed", "other", "reclass",
                ]
                ordered_keys = [key for key in preferred_order if key in tags]
                ordered_keys += [key for key in tags.keys() if key not in ordered_keys]

                # 所有 namespace 共用同一个值起点。QPlainTextEdit 的制表位使用
                # 像素距离，正好适合在比例字体和高 DPI 下保持换行后的视觉对齐。
                fm = QFontMetrics(self.details.font())
                visible_keys = []
                for key in ordered_keys:
                    values = tags.get(key)
                    if isinstance(values, str):
                        values = [values]
                    if isinstance(values, list) and any(str(v).strip() for v in values):
                        visible_keys.append(key)
                max_prefix = max(
                    (fm.horizontalAdvance(f"  {key}:") for key in visible_keys),
                    default=fm.horizontalAdvance("  character:"),
                )
                tag_tab_stop = max_prefix + fm.horizontalAdvance("  ")
                self.details.setTabStopDistance(float(tag_tab_stop))

                for key in ordered_keys:
                    values = tags.get(key)
                    if isinstance(values, str):
                        values = [values]
                    if not isinstance(values, list):
                        continue
                    clean_values = [str(v).strip() for v in values if str(v).strip()]
                    if clean_values:
                        self._append_wrapped_tag_line(lines, key, clean_values, tag_tab_stop)
            else:
                lines.append("  (无)")

            lines += [
                "",
                f"数据库 filepath：{record.filepath}",
            ]

        manual_text = item.manual_name if item.manual_name else "(尚无手动版本)"
        source_text = "手动版本" if item.name_source == "manual" else "程序建议"
        lines += [
            "",
            "──────── 本地 / 编辑状态 ────────",
            f"内部编号：{item.local_id}",
            f"处理建议：{item.category}",
            f"作品属性：{', '.join(item.attributes) if item.attributes else '(无)'}",
            f"人工修改：{'是' if (item.manual_edited or item.has_manual_version) else '否'}",
            f"人工确认：{'是' if item.manual_confirmed else '否'}",
            f"确认名称：{item.confirmed_name if item.manual_confirmed else '(未确认)'}",
            f"AI审查：{item.ai_status}",
            f"AI最后审查：{item.ai_reviewed_at or '-'}",
            f"AI模型：{item.ai_model or '-'}",
            f"AI审查次数：{item.ai_review_count}",
            f"程序建议：{item.program_suggested_name}{item.suffix}",
            f"手动版本：{manual_text}{item.suffix if item.manual_name else ''}",
            f"当前使用：{source_text}",
            f"匹配方式：{item.match_method or '-'}",
            f"相对路径：{item.extra.get('relative_path') or '(根目录直接项目)'}",
            f"当前实际路径：{item.original_path}",
            f"文件操作状态：{item.file_status}",
        ]
        result = item.ai_review_result or {}
        if result:
            lines.extend(["", "──────── 最近一次 AI 结果 ────────",
                          f"名称结论：{result.get('decision', '-')}",
                          f"理由：{result.get('reason', '-')}",
                          f"应用情况：{result.get('apply', '-')}"])
        if item.ai_status == AI_FAILED:
            lines.append(f"审核失败：{(item.extra or {}).get('last_ai_failure', '可在 AI 任务中查看原因')}")
        self.details.setPlainText("\n".join(lines))
        self._update_name_version_button(item)
        self._update_confirmation_button(item)

    def _update_confirmation_button(self, item: WorkItem | None):
        if not hasattr(self, "confirm_name_btn"):
            return
        self.confirm_name_btn.setText("标记为已确认")
        self.confirm_name_btn.setEnabled(item is not None and item.category != CATEGORY_CONFIRMED)

    def toggle_current_confirmation(self):
        item = self._current_item()
        if not item:
            return
        if item.category == CATEGORY_CONFIRMED:
            return

        def mutate(target: WorkItem):
            self._mark_item(target, CATEGORY_CONFIRMED)
            return True

        self._apply_batch_mutation(
            "标记为“已确认”",
            (item.local_id,),
            mutate,
            undo_kind="classification",
        )

    def toggle_name_version(self):
        scope = self.current_category()
        view = self.current_view()
        model = self.current_model()
        index = view.currentIndex()
        if not index.isValid():
            return
        item = index.data(CardListModel.ITEM_ROLE)
        if not item or not item.has_manual_version:
            return
        local_id = item.local_id
        if item.name_source == "manual":
            model.switch_to_program(index.row())
        else:
            model.switch_to_manual(index.row())
        # itemEdited may rebuild a search result model; use local_id rather
        # than the old (possibly invalid) QModelIndex to restore the same comic.
        self._restore_current_context(scope, local_id)

    # ───────────────── 执行预检 / 模拟执行 ─────────────────
    @staticmethod
    def _looks_windows_path(path: str) -> bool:
        value = str(path or "")
        return bool(re.match(r"^[A-Za-z]:[\\/]", value) or "\\" in value)

    def _join_target_path(self, parent: str, name: str) -> str:
        if self._looks_windows_path(parent):
            return ntpath.normpath(ntpath.join(parent, name))
        return os.path.normpath(os.path.join(parent, name))

    def _target_path_for_item(self, item: WorkItem) -> str:
        final_name = item.final_name_with_suffix
        if self.in_place_radio.isChecked():
            if self._looks_windows_path(item.original_path):
                parent = ntpath.dirname(item.original_path)
            else:
                parent = os.path.dirname(item.original_path)
        else:
            parent = self.dest_edit.text().strip()
        return self._join_target_path(parent, final_name) if parent else final_name

    @staticmethod
    def _windows_path_key(path: str) -> str:
        return path_key(path)

    @staticmethod
    def _windows_name_problem(name: str) -> str:
        return name_problem(name)

    def _selected_items_for_execution(self, notice: bool = True) -> list[WorkItem]:
        items = self.selected_items_current_scope()
        if not items:
            if notice:
                QMessageBox.information(self, "没有选择", "请先在当前标签页勾选至少一个漫画。")
            return []
        return list(items)

    def _execution_signature(self, items: list[WorkItem] | None = None):
        items = items if items is not None else self._selected_items_for_execution(notice=False)
        mode = "in_place" if self.in_place_radio.isChecked() else "move"
        dest = self.dest_edit.text().strip() if mode == "move" else ""
        rows = tuple(sorted((x.local_id, x.suggested_name, self._target_path_for_item(x)) for x in items))
        return mode, dest, rows

    def _invalidate_execution_validation(self, *_args):
        self._conflict_validation_signature = None
        self._preview_validation_signature = None
        if hasattr(self, "preview_btn"):
            self.preview_btn.setEnabled(False)
        if hasattr(self, "execute_btn"):
            self.execute_btn.setEnabled(False)
        if self._file_dialog is not None:
            self._file_dialog.schedule_refresh()

    def _build_operation_rows(self, items: list[WorkItem]) -> list[dict]:
        rows: list[dict] = []
        source_by_key = {self._windows_path_key(x.original_path): x for x in items}
        target_groups: dict[str, list[WorkItem]] = defaultdict(list)
        target_for_id: dict[str, str] = {}
        for item in items:
            target = self._target_path_for_item(item)
            target_for_id[item.local_id] = target
            target_groups[self._windows_path_key(target)].append(item)

        duplicate_keys = {key for key, group in target_groups.items() if len(group) > 1}
        for item in items:
            target = target_for_id[item.local_id]
            target_key = self._windows_path_key(target)
            source_key = self._windows_path_key(item.original_path)
            changed = target != item.original_path
            blocking = False
            kind = ""
            messages: list[str] = []

            problem = self._windows_name_problem(item.final_name_with_suffix)
            if problem:
                blocking = True
                kind = "invalid_name"
                messages.append(problem)

            if target_key in duplicate_keys:
                blocking = True
                kind = "batch_duplicate"
                messages.append(f"本批有 {len(target_groups[target_key])} 项最终目标相同")

            # 目标路径现存：如果它正好是本批另一项目的源路径且对方会移走，属于临时占位；
            # 否则视为外部目标冲突。当前路径本身（原地名称不变）不算冲突。
            try:
                exists = os.path.exists(target)
            except Exception:
                exists = False
            if target_key != source_key and exists:
                occupying = source_by_key.get(target_key)
                if occupying is not None and self._windows_path_key(target_for_id.get(occupying.local_id, occupying.original_path)) != target_key:
                    if not blocking:
                        kind = "temporary_occupancy"
                    messages.append("目标暂由本批另一项目占用；未来执行层可用临时名安全换位")
                else:
                    blocking = True
                    kind = "target_exists"
                    messages.append("目标位置已经存在同名文件/文件夹")

            if blocking:
                status = "✕ 冲突"
            elif kind == "temporary_occupancy":
                status = "⚠ 可处理占位"
            elif changed:
                status = "✓ 可模拟"
            else:
                status = "✓ 名称不变"

            rows.append({
                "local_id": item.local_id,
                "item": item,
                "status": status,
                "current_name": f"{item.original_name}{item.suffix}",
                "final_name": item.final_name_with_suffix,
                "target_path": target,
                "message": "；".join(messages) if messages else "-",
                "blocking_conflict": blocking,
                "conflict_kind": kind,
                "changed": changed,
            })
        return rows

    def _show_operation_rows(self, rows: list[dict], title: str, conflicts_only: bool = False):
        dialog = OperationPreviewDialog(rows, title=title, parent=self)
        if conflicts_only:
            dialog.filter_combo.setCurrentIndex(2)
        dialog.exec()

    def test_name_conflicts(self):
        items = self._selected_items_for_execution()
        if not items:
            return
        if self.move_radio.isChecked() and not self.dest_edit.text().strip():
            QMessageBox.warning(self, "未选择目标目录", "当前为“修改后移动到其他目录”，请先选择目标目录。")
            return
        rows = self._build_operation_rows(items)
        conflicts = [r for r in rows if r.get("blocking_conflict")]
        temporary = [r for r in rows if r.get("conflict_kind") == "temporary_occupancy"]
        safe_count = len(rows) - len(conflicts)

        if not conflicts:
            self._conflict_validation_signature = self._execution_signature(items)
            self._preview_validation_signature = None
            self.preview_btn.setEnabled(True)
            QMessageBox.information(
                self, "重名检测通过",
                f"已检查 {len(items)} 项。\n\n阻断冲突：0\n"
                f"可自动处理的临时占位：{len(temporary)}\n可进入模拟清单：{safe_count} 项。"
            )
            return

        box = QMessageBox(self)
        box.setWindowTitle("发现目标冲突")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            f"已检查 {len(items)} 项。\n\n"
            f"阻断冲突：{len(conflicts)} 项\n可继续的无冲突项：{safe_count} 项\n"
            f"可自动处理的临时占位：{len(temporary)} 项"
        )
        view_btn = box.addButton("查看冲突项", QMessageBox.ButtonRole.ActionRole)
        continue_btn = box.addButton("继续处理无冲突部分", QMessageBox.ButtonRole.AcceptRole)
        cancel_btn = box.addButton("取消", QMessageBox.ButtonRole.RejectRole)
        continue_btn.setEnabled(safe_count > 0)
        box.exec()

        if box.clickedButton() is view_btn:
            self._show_operation_rows(rows, "重名检测结果", conflicts_only=True)
            self._invalidate_execution_validation()
            return
        if box.clickedButton() is continue_btn:
            conflict_ids = {r["local_id"] for r in conflicts}
            for item in self.all_items:
                if item.local_id in conflict_ids:
                    item.checked = False
            self.current_model().refresh_checks()
            self.update_selected_count()
            remaining = self._selected_items_for_execution(notice=False)
            self._conflict_validation_signature = self._execution_signature(remaining)
            self._preview_validation_signature = None
            self.preview_btn.setEnabled(bool(remaining))
            self._show_status_message(
                f"已排除 {len(conflicts)} 个冲突项；剩余 {len(remaining)} 项通过重名检测。", 5000
            )
            return
        self._invalidate_execution_validation()

    def preview_operations(self):
        items = self._selected_items_for_execution()
        if not items:
            return
        signature = self._execution_signature(items)
        if self._conflict_validation_signature != signature:
            self._invalidate_execution_validation()
            QMessageBox.information(self, "需要重新测试重名", "当前选择、名称或目标设置已经变化。请先完成“② 测试重名”。")
            return
        rows = self._build_operation_rows(items)
        if any(r.get("blocking_conflict") for r in rows):
            self._invalidate_execution_validation()
            QMessageBox.warning(self, "预检状态已失效", "模拟前重新发现了阻断冲突，请重新执行“② 测试重名”。")
            return
        self._show_operation_rows(rows, "模拟操作清单（不会执行）")
        self._preview_validation_signature = signature
        self.execute_btn.setEnabled(False)
        self._show_status_message(
            f"模拟清单已生成：{len(rows)} 项。联锁已到第③步；V{VERSION} 仍禁用真实执行。", 6000
        )

    def _update_execute_state(self):
        # V0.2.26 只完成联锁、重名检测和模拟，不允许真实修改硬盘。
        if hasattr(self, "execute_btn"):
            self.execute_btn.setEnabled(False)

    # ───────────────── V0.3.1 API 复核 ─────────────────
    def open_ai_dialog(self):
        perf = self._perf_diag
        started = perf_counter() if perf else 0
        stage = started
        if perf:
            self._ai_open_sequence = getattr(self, '_ai_open_sequence', 0) + 1
            opening = self._ai_open_sequence
            perf.record('ai.open.request', opening=opening)
        repository = getattr(self, '_txt_repository', None)
        if repository is None:
            repository = self.txt_repository()  # The initial accessor already scans.
        else:
            repository.scan()
        if perf:
            stage = perf.mark('ai.open.history', stage, opening=opening,
                              tasks=len(repository.tasks(self.ai_tasks)))
        created = self._ai_dialog is None
        if self._ai_dialog is None:
            self._ai_dialog = AiDialog(self)
        else:
            self._ai_dialog.refresh()
        if perf:
            stage = perf.mark('ai.open.construct' if created else 'ai.open.refresh',
                              stage, opening=opening)
            perf.watch_dialog_open(self._ai_dialog, started, opening)
        self._ai_dialog.show()
        self._ai_dialog.raise_()
        if perf:
            perf.mark('ai.open.show', stage, opening=opening)
            perf.mark('ai.open.total', started, opening=opening, created=created)
            QTimer.singleShot(0, lambda p=perf, t=started, n=opening:
                              p.mark('ai.open.next_event_turn', t, opening=n))

    def start_api_checked(self):
        ids = tuple(item.local_id for item in self.selected_items_current_scope())
        if not ids:
            QMessageBox.information(self, "没有勾选漫画", "请先在当前分类或搜索结果里勾选漫画。")
            return
        config = self.current_api_config()
        try:
            validate_config(config)
        except ValueError:
            self.open_ai_dialog()
            self._ai_dialog.prepare_checked(ids)
            self._ai_dialog.show_settings()
            return
        self.start_api_from_scope("checked", [], ids)

    def ai_items_for_scope(self, scope: str, categories: list[str] | None = None,
                           prepared_ids: tuple[str, ...] | None = None) -> list[WorkItem]:
        if scope == "ids":
            ids = set(prepared_ids or ())
            return [item for item in self.all_items if item.local_id in ids]
        if scope == "checked":
            ids = set(prepared_ids) if prepared_ids is not None else None
            return [item for item in self._scope_items() if item.checked] if ids is None else [
                item for item in self._scope_items() if item.local_id in ids and item.checked
            ]
        if scope == "categories":
            chosen = set(categories or [])
            return [item for item in self.all_items if item.category in chosen]
        if scope == "search":
            return list(self.search_results) if self._search_query else []
        if scope == "all":
            return list(self.all_items)
        return []

    def api_profiles(self) -> tuple[list[dict], str]:
        return profile_state(self.settings_data)

    def current_api_config(self) -> dict:
        return active_config(self.settings_data)

    def _persist_api_profiles(self, profiles: list[dict], active: str, price_presets=None):
        candidate = saved_state(self.settings_data, profiles, active)
        if price_presets is not None:
            from .ai_cost import validate_presets
            candidate['ai_price_presets'] = validate_presets(price_presets)
        for profile in profiles:
            register_secret(profile.get("key", ""))
        path, temp = self.settings_path(), None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                             prefix=".api_settings_", suffix=".tmp", delete=False) as stream:
                temp = Path(stream.name)
                json.dump(candidate, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        except OSError:
            raise OSError("无法保存 API 设置，请检查程序目录写入权限；原配置仍保留") from None
        finally:
            if temp is not None:
                try:
                    temp.unlink(missing_ok=True)
                except OSError:
                    pass
        self.settings_data = candidate
        self._refresh_ai_dialog()

    def save_api_config(self, config: dict, profile_id: str | None = None) -> str:
        validate_config(config)
        profiles, active = self.api_profiles()
        # None keeps the old single-argument caller's update behavior; an empty
        # id is an explicit new unsaved profile from the settings page.
        target = active if profile_id is None else profile_id
        old = next((p for p in profiles if p["id"] == target), None)
        if target and old is None:
            raise ValueError("这套配置已不存在，请重新选择")
        name = str(config.get("name", (old or {}).get("name", "API 配置"))).strip()
        if not name or len(name) > 80:
            raise ValueError("请填写 1 到 80 个字符的配置名称")
        if any(p["id"] != target and p["name"].casefold() == name.casefold() for p in profiles):
            raise ValueError("配置名称已存在，请换一个便于区分的名称")
        target = target or new_profile_id()
        profile = {"id": target, "name": name,
                   **{field: str(config.get(field, "")).strip() for field in ("endpoint", "key", "model")},
                   "provider": config.get("provider", "auto"), "thinking_mode": thinking_mode(config),
                   **runtime_values(config), "effort": config.get("effort", "default"),
                   "pricing": copy.deepcopy(config.get("pricing", {}))}
        from .ai_config import default_max_tokens
        profile['max_tokens'] = config.get('max_tokens',default_max_tokens(config))
        profile['custom_parameters'] = copy.deepcopy(config.get('custom_parameters',{}))
        profile['price_preset_id'] = str(config.get('price_preset_id',''))
        profiles = [profile if p["id"] == target else p for p in profiles]
        if old is None:
            profiles.append(profile)
        self._persist_api_profiles(profiles, target, config.get('price_presets'))
        return target

    def save_ai_supplements(self, rows):
        from .ai_txt import atomic_text
        candidate = copy.deepcopy(self.settings_data)
        candidate["ai_supplements"] = validate_supplements(rows)
        try:
            atomic_text(self.settings_path(), json.dumps(candidate, ensure_ascii=False, indent=2))
        except OSError:
            raise OSError("补充依据无法保存，原设置保留；请检查目录权限") from None
        self.settings_data = candidate
        self._refresh_ai_dialog()

    def select_api_profile(self, profile_id: str):
        profiles, _ = self.api_profiles()
        if not any(p["id"] == profile_id for p in profiles):
            raise ValueError("这套配置已不存在，请重新选择")
        self._persist_api_profiles(profiles, profile_id)

    def delete_api_profile(self, profile_id: str):
        profiles, active = self.api_profiles()
        if not any(p["id"] == profile_id for p in profiles):
            raise ValueError("这套配置已不存在，请重新选择")
        profiles = [p for p in profiles if p["id"] != profile_id]
        if active == profile_id:
            active = profiles[0]["id"] if profiles else ""
        self._persist_api_profiles(profiles, active)

    def _task_by_id(self, task_id: str) -> dict | None:
        return next((task for task in self.ai_tasks if task.get("task_id") == task_id), None)

    def start_api_from_scope(self, scope: str, categories: list[str] | None = None,
                             prepared_ids: tuple[str, ...] | None = None,
                             continuation: str = ""):
        self._commit_active_edit()
        config = self.current_api_config()
        try:
            validate_config(config)
        except ValueError as exc:
            self.open_ai_dialog()
            self._ai_dialog.prepare_checked(prepared_ids or ()) if scope == "checked" else None
            self._ai_dialog.show_settings()
            self._ai_dialog.config_note.setText(str(exc))
            return
        items = self.ai_items_for_scope(scope, categories, prepared_ids)
        self._ensure_library()
        items = self._filter_ai_file_overlap(items)
        items = sort_items({item.local_id: item for item in items}.values())
        if not items:
            QMessageBox.information(self, "没有送审漫画", "当前选择范围里没有可送审的漫画。")
            return
        continuation_task = self._task_by_id(continuation) if continuation else None
        source_is_txt = bool(continuation_task and continuation_task.get('transport')=='TXT')
        active_ids = {
            local_id for task in self.ai_tasks if (task.get("task_id") != continuation or source_is_txt) and task.get("state") in {"running", "queued", "paused", "interrupted", "waiting", "export_failed"}
            for local_id, row in task.get("items", {}).items()
            if row.get("state") in {"pending", "dispatching", "uncertain", "export_pending"}
        }
        overlap = active_ids.intersection(item.local_id for item in items)
        if overlap:
            box = QMessageBox(self)
            box.setWindowTitle("有漫画正在审核")
            box.setText(f"其中 {len(overlap)} 本与未完成的任务重叠。全部继续后以新API轮次为准，旧结果晚到将忽略。")
            all_button = box.addButton("全部继续", QMessageBox.AcceptRole)
            skip_button = box.addButton("只发无重叠项", QMessageBox.ActionRole)
            box.addButton("取消", QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is skip_button:
                items = [item for item in items if item.local_id not in overlap]
            elif box.clickedButton() is not all_button:
                return
            if not items:
                QMessageBox.information(self, "没有新项目", "所选漫画都在未完成的审核任务中。")
                return

        if len({item.local_id for item in self.all_items}) != len(self.all_items):
            QMessageBox.warning(self, "漫画身份重复", "当前漫画内部编号有重复；请先重新扫描以保证安全匹配。")
            return
        task_id = uuid.uuid4().hex
        prior_task = self._task_by_id(continuation) if continuation else None
        supplements = copy.deepcopy(prior_task.get("supplements", [])) if prior_task else enabled_supplements(self.settings_data)
        backup = [(item, item.review_round, item.ai_special_flow) for item in items]
        old_tasks = copy.deepcopy(self.ai_tasks)
        old_superseded = set(self._ai_runner.superseded_ids) if self._ai_runner else None
        task = {"task_id": task_id, "session_id": self.session_id, "library_id": self.library_id,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "state": "queued", "scope": scope, "continuation": continuation,
                "config": runtime_config(config),
                "items": {}, "events": [], "requests": [], "supplements": supplements}
        for item in items:
            item.review_round += 1
            item.ai_special_flow = item.ai_special_flow or item.category in {CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED} or item.program_category in {CATEGORY_LLM_REVIEW, CATEGORY_AI_REVIEWED}
            entry = input_item(item, task_id, item.review_round, supplements)
            task["items"][item.local_id] = {"state": "pending", "input": entry,
                                                 "original_path": item.original_path, "reason": ""}
            for prior in self.ai_tasks:
                old = prior.get("items", {}).get(item.local_id)
                if old and old.get("state") in {"pending", "dispatching", "uncertain", "export_pending"}:
                    old["state"] = "superseded"
                    old["reason"] = "已由较新审核轮次接替"
                    if self._ai_runner and prior.get("task_id") == self._ai_runner.task_id:
                        self._ai_runner.superseded_ids.add(item.local_id)
        from .ai_txt import task_state
        for prior in self.ai_tasks:
            if prior.get("transport") == "TXT":
                prior["state"] = task_state(prior)
        self.ai_tasks.append(task)
        try:
            self._save_current_session(strict=True, stage="创建任务", task_id=task_id)
        except Exception:
            self.ai_tasks = old_tasks
            for item, number, flow in backup:
                item.review_round, item.ai_special_flow = number, flow
            if self._ai_runner and old_superseded is not None:
                self._ai_runner.superseded_ids = old_superseded
            QMessageBox.warning(self, "任务未创建", "无法可靠保存审核范围和输入；没有向 API 发送漫画。")
            return
        self._ai_secret_configs[task_id] = dict(config)
        self._show_status_message(f"已创建 AI 任务：{len(items)} 本。")
        self._refresh_ai_dialog()
        self._launch_next_ai_task()

    def _launch_next_ai_task(self):
        if self._ai_runner is not None:
            return
        task = next((x for x in self.ai_tasks if x.get("state") == "queued" and x["task_id"] in self._ai_secret_configs), None)
        if not task:
            self._ai_runner = None
            return
        entries = [row["input"] for row in task["items"].values() if row["state"] == "pending"]
        task["state"] = "running"
        try:
            self._save_current_session(strict=True, stage="启动任务", task_id=task["task_id"])
        except Exception:
            task["state"] = "queued"
            QMessageBox.warning(self, "任务未启动", "无法保存任务运行状态；没有向 API 发送漫画。")
            return
        runner = ApiRunner(task["task_id"], entries, self._ai_secret_configs[task["task_id"]], self._ai_signals,
                           log_root=self.review_log_root(), continuation=task.get("continuation", ""))
        self._ai_runner = runner
        runner.start()
        self._refresh_library_lock_ui()
        if self._file_dialog is not None:
            self._file_dialog.schedule_refresh()
        self._refresh_ai_dialog()

    def review_log_root(self) -> Path:
        if Path(self.tool_dir()).resolve() == data_dir().resolve():
            return logs_dir()
        return Path(self.tool_dir()) / "logs"

    def review_log_roots(self):
        if Path(self.tool_dir()).resolve() == data_dir().resolve():
            return log_roots()
        return (Path(self.tool_dir()) / "logs",)

    def _ai_log(self, task_id: str) -> TaskLog:
        return TaskLog(self.review_log_root(), task_id,
                       lambda warning: self._on_ai_diagnostic(task_id, warning))

    def _on_ai_diagnostic(self, task_id: str, warning: dict):
        task = self._task_by_id(task_id)
        if warning.get("kind") == "group_phase":
            if task:
                task.setdefault("group_status", {})[str(warning["group"])] = {k:v for k,v in warning.items() if k!="kind"}
            self._refresh_ai_dialog()
            return
        if warning.get("kind") == "in_flight":
            if task:
                task["in_flight_groups"] = warning.get("groups", 0)
            self._ai_in_flight = bool(warning.get("groups"))
            self._refresh_ai_dialog()
            return
        if warning.get("kind") == "task_timing":
            if task:
                task["elapsed_ms"] = warning.get("elapsed_ms")
            return
        if warning.get("kind") == "request_metric":
            if task:
                metric = warning["metric"]
                requests = task.setdefault("requests", [])
                existing = next((i for i, value in enumerate(requests)
                                 if value.get("request_id") == metric["request_id"]), None)
                if existing is None:
                    requests.append(metric)
                else:
                    requests[existing] = metric
                self._refresh_ai_dialog()
            return
        if warning.get("kind") == "split_notice":
            message = str(warning.get("message", ""))
            if task:
                task["events"].append({"at": datetime.now().isoformat(timespec="seconds"),
                                       "event": "截断拆组", "message": message})
            self._show_status_message(message, 6000)
            self._refresh_ai_dialog()
            return
        message = _redact(str(warning.get("message") or "AI 日志未保存"))
        if task:
            messages = task.setdefault("diagnostic_warnings", [])
            if message not in messages:
                messages.append(message)
        self._show_status_message(message + "；请查看任务详情。", 10000)
        self._refresh_ai_dialog()

    def open_task_log_folder(self, task_id: str):
        path = next((task_log_dir(root,task_id) for root in self.review_log_roots() if task_log_dir(root,task_id).is_dir()),task_log_dir(self.review_log_root(),task_id))
        if not path.is_dir():
            QMessageBox.information(self, "尚无日志目录", "该任务没有本版生成的日志目录；旧版任务没有保存原始返回。")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.warning(self, "无法打开日志目录", f"请手工打开：\n{path}")

    def _on_ai_prepare(self, task_id: str, number: int, group: list[dict]):
        runner = self._ai_runner
        task = self._task_by_id(task_id)
        if not runner or not task or runner.task_id != task_id:
            return
        before = copy.deepcopy(task)
        for entry in group:
            row = task["items"][entry["CONTROL"]["local_id"]]
            if row["state"] == "pending":
                row["state"] = "dispatching"
        task["events"].append({"group": number, "at": datetime.now().isoformat(timespec="seconds"),
                               "event": "准备发送", "local_ids": [x["CONTROL"]["local_id"] for x in group]})
        try:
            self._save_current_session(strict=True, stage=f"准备发送第 {number} 组", task_id=task_id)
        except Exception:
            task.clear(); task.update(before)
            QMessageBox.warning(self, "任务保存失败", "本组尚未发送；无法确认保存，已停止后续请求。")
            runner.acknowledge(False)
            return
        self._ai_in_flight = True
        self._ai_log(task_id).summary(task)
        runner.acknowledge(True)
        self._refresh_ai_dialog()

    def _on_ai_group_ready(self, task_id: str, number: int, results: dict, global_error: bool):
        runner = self._ai_runner
        task = self._task_by_id(task_id)
        if not runner or not task or runner.task_id != task_id:
            return
        self._ai_in_flight = bool(runner.active_groups)
        apply_started = perf_counter()
        self._commit_active_edit()
        old_task = copy.deepcopy(task)
        if len({x.local_id for x in self.all_items}) != len(self.all_items):
            QMessageBox.warning(self, "任务身份异常", "漫画内部编号重复；本组结果未应用，停止后续发送。")
            runner.acknowledge(False)
            return
        by_id = {item.local_id: item for item in self.all_items}
        before_items = {local_id: copy.deepcopy(by_id[local_id].__dict__) for local_id in results if local_id in by_id}
        for local_id, output in results.items():
            row = task["items"].get(local_id)
            if not row or row["state"] == "superseded":
                continue
            row["attempts"] = int(output.get("attempts", 1))
            if output.get("not_sent"):
                row["state"], row["reason"] = "pending", "已停止，尚未发送"
                continue
            item = by_id.get(local_id)
            if (task.get("session_id") != self.session_id or task.get('library_id', self.library_id) != self.library_id or not item
                    or item.original_path != row.get("original_path")
                    or not item.original_path or not os.path.exists(item.original_path)):
                row["state"], row["reason"] = "missing", "原项目已不存在，未匹配到其他漫画"
                continue
            original = row["input"]
            control = original["CONTROL"]
            if item.review_round != control["review_round"]:
                row["state"], row["reason"] = "superseded", "较新审核轮次已接替"
                continue
            if current_input_fingerprint(item, original) != control["input_fingerprint"]:
                output = {"error": "stale_result：送审后名称或来源资料已变化"}
            apply_review_output(item, row, output, task)
        task.setdefault("group_status", {})[str(number)] = {"phase":"saved"}
        task["events"].append({"group": number, "at": datetime.now().isoformat(timespec="seconds"),
                               "event": "全局错误，暂停" if global_error else "结果已核验"})
        self._ai_log(task_id).event("group_applied", group=number,
            apply_ms=round((perf_counter() - apply_started) * 1000, 2),
            results={local_id: {key: task["items"].get(local_id, {}).get(key)
                               for key in ("state", "decision", "reason", "apply", "error_code")}
                     for local_id in results})
        try:
            self._save_current_session(strict=True, stage=f"提交第 {number} 组结果", task_id=task_id)
        except Exception:
            task.clear(); task.update(old_task)
            for local_id, state in before_items.items():
                by_id[local_id].__dict__.clear(); by_id[local_id].__dict__.update(state)
            QMessageBox.warning(self, "结果尚未确认保存", "API 已返回，但关键结果写入失败；已停止后续发送。具体异常已尝试写入任务日志，请保留当前程序状态。")
            runner.acknowledge(False)
            return
        self._ai_log(task_id).summary(task)
        runner.acknowledge(True)
        anchor = self._capture_page_anchor()
        self.populate_categories(anchor=anchor, preserve_current=True, background_ai=True)
        self.show_scan_summary()
        self._refresh_ai_dialog()

    def _on_ai_done(self, task_id: str, state: str):
        task = self._task_by_id(task_id)
        final_state_saved = True
        if task:
            previous_state = task["state"]
            task["state"] = state
            task["in_flight_groups"] = 0
            task["cost_estimate"] = estimate_cost(task)
            if state == "save_failed":
                for row in task.get("items", {}).values():
                    if row.get("state") == "dispatching":
                        row["state"], row["reason"] = "uncertain", "结果写入未确认；需明确接续"
            try:
                self._save_current_session(strict=True, stage="任务结束状态", task_id=task_id)
            except Exception:
                task["state"] = "save_failed"
                task["persistence_warning"] = f"任务结束状态未确认写入；磁盘保留此前 {previous_state} 状态。"
                final_state_saved = False
                QMessageBox.warning(self, "任务状态未保存", "本组结果按前次保存保留，但任务结束状态未确认写入。")
        self._ai_log(task_id).event("task_finish_commit", requested_state=state,
                                    final_state_saved=final_state_saved)
        if task:
            self._ai_log(task_id).summary(task, final_state_saved=final_state_saved)
        self._ai_in_flight = False
        self._ai_runner = None
        self._ai_secret_configs.pop(task_id, None)
        self._refresh_library_lock_ui()
        if self._file_dialog is not None:
            self._file_dialog.schedule_refresh()
        self._refresh_ai_dialog()
        if self._tasks_exit_pending:
            self._maybe_exit_after_tasks()
        elif self._ai_exit_after_group and final_state_saved and state not in {"save_failed", "interrupted"}:
            self._ai_exit_after_group = False
            self._ai_closing = True
            self.close()
        else:
            self._ai_exit_after_group = False
            if final_state_saved and state not in {"save_failed", "interrupted"}:
                self._launch_next_ai_task()

    def stop_api_task(self):
        if self._ai_runner and self._ai_runner.is_alive():
            self._ai_runner.stop_after_group.set()
            self._show_status_message("完成已发送组并保存后停止，不再派发新组。", 6000)

    def resume_api_task(self, task_id: str, selected=None):
        from .ai_remaining import remaining_ids
        from .ai_txt_store import accessible
        task = self._task_by_id(task_id)
        if not task:
            return
        if self._ai_runner and self._ai_runner.task_id==task_id and self._ai_runner.is_alive():
            QMessageBox.information(self, "任务仍在运行", "请先停止派发，等待已发送组保存后再处理剩余项。")
            return
        self._commit_active_edit()
        if not accessible(self.work_edit.text().strip()):
            QMessageBox.warning(self, "原工作目录不可用", "恢复原工作目录后再复核剩余项。")
            return
        by_id = {item.local_id: item for item in self.all_items}
        ids = remaining_ids(task, by_id, selected)
        if not ids:
            QMessageBox.information(self, "没有剩余项目", "所选范围没有仍属当前轮次、资料未变化且原文件可访问的剩余漫画。资料变化项请新建复核。")
            return
        self.start_api_from_scope("ids", [], ids, continuation=task_id)

    def _refresh_ai_dialog(self):
        if self._ai_dialog is not None:
            self._ai_dialog.refresh()

    def _exit_ai_now(self) -> bool:
        runner = self._ai_runner
        task = self._task_by_id(runner.task_id) if runner else None
        if not runner or not task:
            return True
        previous = copy.deepcopy(task)
        task["state"] = "interrupted"
        for row in task.get("items", {}).values():
            if row.get("state") == "dispatching":
                row["state"] = "uncertain"
                row["reason"] = "退出时请求状态未确认；下次须明确选择接续"
        try:
            self._save_current_session(strict=True)
        except Exception:
            task.clear(); task.update(previous)
            QMessageBox.warning(self, "无法安全退出", "任务中断状态无法保存，请保持程序打开并检查磁盘。")
            return False
        self._ai_closing = True
        self._ai_exit_after_group = False
        runner.abandon.set()
        runner.acknowledge(False)
        return True

    def closeEvent(self, event):
        dialog = getattr(self, '_ai_dialog', None)
        if dialog is not None and not dialog._guard_changes('关闭'):
            event.ignore()
            return
        if not self._guard_active_tasks_exit(event):
            return
        # 人工内容先落盘，再保存纯界面设置。
        if self._ai_dialog is not None:
            self._ai_dialog._save_comic_column_widths()
        if self.all_items:
            self._save_current_session()
        self._finish_background_session_save()
        if self._session_save_executor is not None:
            self._session_save_executor.shutdown(wait=True)
            self._session_save_executor = None
        self.settings_data["library_lock"] = self._library_lock_active()
        self.settings_data["source_database"] = self.db_edit.text().strip()
        self.settings_data["work_directory"] = self.work_edit.text().strip()
        self.settings_data["destination_directory"] = self.dest_edit.text().strip()
        self.settings_data["control_dock_visible"] = self.control_dock.isVisible()
        self.settings_data["control_dock_floating"] = self.control_dock.isFloating()
        try:
            if self.control_dock.isFloating():
                self.settings_data["control_dock_float_geometry"] = bytes(
                    self.control_dock.saveGeometry().toBase64()
                ).decode("ascii")
            else:
                self.settings_data["control_dock_width"] = self.control_dock.width()
                self.settings_data.pop("control_dock_float_geometry", None)
        except Exception:
            pass
        self.settings_data["detail_dock_visible"] = self.detail_dock.isVisible()
        self.settings_data["detail_dock_floating"] = self.detail_dock.isFloating()
        try:
            if self.detail_dock.isFloating():
                self.settings_data["detail_dock_float_geometry"] = bytes(
                    self.detail_dock.saveGeometry().toBase64()
                ).decode("ascii")
            else:
                self.settings_data.pop("detail_dock_float_geometry", None)
        except Exception:
            pass
        try:
            self.settings_data["main_window_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        except Exception:
            pass
        self.settings_data["control_sections"] = {
            key: section.is_expanded() for key, section in self.sections.items()
        }
        self._save_settings_file()
        super().closeEvent(event)
        if self._perf_diag:
            try:
                path = self._perf_diag.flush()
                log_message(f"PERF-27-01 诊断日志已保存：{path.name}")
            except Exception as exc:
                log_message(f"PERF-27-01 诊断日志保存失败：{type(exc).__name__}", "WARNING")
