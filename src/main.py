from __future__ import annotations

import sys
import traceback

from .run_log import log_exception, log_message, start_run_log
from .version import VERSION


# 日志必须尽可能早地建立，这样连 PySide6 / 主窗口导入失败也能留下当前启动批次日志。
RUN_LOG_PATH = start_run_log() if __name__ != "__mp_main__" else None


def _global_excepthook(exc_type, exc_value, exc_tb) -> None:
    text = log_exception(exc_type, exc_value, exc_tb, "未处理异常")
    try:
        print(text, file=sys.stderr)
    except Exception:
        pass


sys.excepthook = _global_excepthook


try:
    from PySide6.QtWidgets import QApplication
    from .main_window import MainWindow
except Exception:
    error = log_exception(prefix="启动导入失败")
    print(error, file=sys.stderr)
    raise


def main():
    try:
        app = QApplication(sys.argv)
        app.setApplicationName("漫画批量改名工具")
        app.setApplicationVersion(VERSION)
        app.setOrganizationName("LocalMangaTools")
        safe_layout = "--safe-layout" in sys.argv
        window = MainWindow(safe_layout=safe_layout)
        window.show()
        if RUN_LOG_PATH is None:
            window._show_status_message("运行日志无法创建；请检查程序日志目录的写入权限。", 12000)
        log_message("主窗口已启动。")
        rc = app.exec()
        log_message(f"程序正常退出，exit code={rc}。")
        return rc
    except Exception:
        error = log_exception(prefix="主程序异常退出")
        print(error, file=sys.stderr)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
