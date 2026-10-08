"""Small native-style adjustments shared by the main window and review pages."""
from PySide6.QtCore import Qt, QRectF, QObject, QEvent, QRect
from PySide6.QtGui import QColor, QPainter, QPen, QPalette
from PySide6.QtWidgets import (QApplication, QProxyStyle, QStyle, QLineEdit, QStyleOptionButton,
    QWidget, QSizePolicy, QPushButton, QToolButton, QComboBox, QSplitter, QSplitterHandle,
    QPlainTextEdit, QTextEdit, QStyleOptionFrame, QAbstractSpinBox, QStylePainter, QStyleOptionComboBox)

CONTROL_BORDER = '#969da6'
PANEL_BACKGROUND = '#f5f5f5'


def input_text_inset(widget):
    """One logical text origin, scaling with the inherited font."""
    return max(10, widget.fontMetrics().horizontalAdvance('一'))


def combo_text_rect(widget, option):
    arrow = widget.style().subControlRect(QStyle.CC_ComboBox, option, QStyle.SC_ComboBoxArrow, widget)
    inset = input_text_inset(widget)
    rect = widget.rect().adjusted(inset, 0, -inset, 0)
    rect.setRight(min(rect.right(), arrow.left()-4))
    return rect


class AlignedComboBox(QComboBox):
    """Keep native popup/editor; paint one text origin even under local QSS."""
    def text_rect(self):
        option = QStyleOptionComboBox(); self.initStyleOption(option)
        return combo_text_rect(self, option)

    def paintEvent(self, event):
        if self.isEditable():
            return super().paintEvent(event)
        option = QStyleOptionComboBox(); self.initStyleOption(option)
        painter = QStylePainter(self); painter.drawComplexControl(QStyle.CC_ComboBox, option)
        rect = self.text_rect(); painter.setFont(self.font()); painter.setClipRect(rect)
        if not option.currentIcon.isNull():
            icon_rect = QRect(rect.left(), rect.center().y()-option.iconSize.height()//2,
                              option.iconSize.width(), option.iconSize.height())
            option.currentIcon.paint(painter, icon_rect)
            rect.adjust(option.iconSize.width()+4, 0, 0, 0)
        text = self.fontMetrics().elidedText(self.currentText(), Qt.ElideRight, rect.width())
        painter.drawItemText(rect, Qt.AlignLeft | Qt.AlignVCenter, option.palette,
                            self.isEnabled(), text, QPalette.ButtonText)


class InputInsetFilter(QObject):
    def eventFilter(self, widget, event):
        if event.type() in (QEvent.Polish, QEvent.Show, QEvent.FontChange, QEvent.StyleChange, QEvent.Resize, QEvent.Move):
            if isinstance(widget, (QComboBox, QAbstractSpinBox)):
                editor = widget.findChild(QLineEdit)
                if editor is not None and editor.font() != widget.font():
                    editor.setFont(widget.font())
            if isinstance(widget, QLineEdit) and widget.alignment() & Qt.AlignLeft:
                # Qt's editor adds two native pixels inside its contents rect.
                if isinstance(widget.parentWidget(), (QComboBox, QAbstractSpinBox)):
                    if widget.font() != widget.parentWidget().font():
                        widget.setFont(widget.parentWidget().font())
                    left = max(0, input_text_inset(widget.parentWidget()) - widget.pos().x() - 2)
                else:
                    option = QStyleOptionFrame(); option.initFrom(widget)
                    option.lineWidth = widget.style().pixelMetric(QStyle.PM_DefaultFrameWidth, option, widget) if widget.hasFrame() else 0
                    native = widget.style().subElementRect(QStyle.SE_LineEditContents, option, widget).left()
                    left = max(0, input_text_inset(widget) - native - 2)
                if widget.textMargins().left() != left:
                    widget.setTextMargins(left, 0, left, 0)
            elif isinstance(widget, (QPlainTextEdit, QTextEdit)):
                margin = max(0, input_text_inset(widget) - widget.viewport().pos().x())
                if widget.document().documentMargin() != margin:
                    widget.document().setDocumentMargin(margin)
        return False


def panel_background(widget):
    palette = widget.palette()
    palette.setColor(QPalette.Window, QColor(PANEL_BACKGROUND))
    palette.setColor(QPalette.Base, QColor('white'))
    widget.setPalette(palette)
    widget.setAutoFillBackground(True)


class ControlBorderStyle(QProxyStyle):
    """Keep native rendering and geometry; overlay enabled borders only."""
    def _border(self, option, painter, widget):
        if widget is not None and widget.property('reviewFoldHeader'):
            return
        if not isinstance(widget,(QPushButton,QToolButton,QComboBox)) or not option.state & QStyle.State_Enabled:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(CONTROL_BORDER), 1))
        painter.setBrush(Qt.NoBrush)
        painter.drawRoundedRect(QRectF(option.rect).adjusted(.5,.5,-.5,-.5),4,4)
        painter.restore()

    def drawPrimitive(self, element, option, painter, widget=None):
        native = self._button_option(option, widget)
        super().drawPrimitive(element, native, painter, widget)
        if element in (QStyle.PE_PanelButtonCommand, QStyle.PE_PanelButtonTool):
            if option.state & QStyle.State_Enabled and option.state & QStyle.State_MouseOver and not option.state & QStyle.State_Sunken:
                painter.save()
                painter.setPen(Qt.NoPen); painter.setBrush(QColor("#d2d5d9"))
                painter.drawRoundedRect(QRectF(option.rect).adjusted(1,1,-1,-1),3,3)
                painter.restore()
            self._border(option, painter, widget)

    def _button_option(self, option, widget):
        if isinstance(widget, QPushButton) and isinstance(option, QStyleOptionButton):
            native = QStyleOptionButton(option)
            native.state &= ~QStyle.State_HasFocus
            native.features &= ~(QStyleOptionButton.DefaultButton | QStyleOptionButton.AutoDefaultButton)
            return native
        return option

    def drawComplexControl(self, control, option, painter, widget=None):
        super().drawComplexControl(control, option, painter, widget)
        if control in (QStyle.CC_ComboBox,QStyle.CC_ToolButton):
            self._border(option, painter, widget)

    def drawControl(self, control, option, painter, widget=None):
        if control == QStyle.CE_ComboBoxLabel and isinstance(widget, QComboBox) and not option.editable:
            rect = combo_text_rect(widget, option)
            painter.save(); painter.setClipRect(rect); painter.setFont(widget.font())
            if not option.currentIcon.isNull():
                icon_rect = QRect(rect.left(), rect.center().y()-option.iconSize.height()//2,
                                  option.iconSize.width(), option.iconSize.height())
                option.currentIcon.paint(painter, icon_rect)
                rect.adjust(option.iconSize.width()+4, 0, 0, 0)
            text = widget.fontMetrics().elidedText(option.currentText, Qt.ElideRight, rect.width())
            self.drawItemText(painter, rect, Qt.AlignLeft | Qt.AlignVCenter, option.palette,
                              bool(option.state & QStyle.State_Enabled), text, QPalette.ButtonText)
            painter.restore()
            return
        super().drawControl(control, self._button_option(option,widget), painter, widget)
        if control==QStyle.CE_PushButton:
            self._border(option,painter,widget)
            if option.state & QStyle.State_HasFocus and option.state & QStyle.State_Enabled:
                painter.save(); painter.setPen(QPen(QColor(CONTROL_BORDER),1,Qt.DotLine))
                painter.setBrush(Qt.NoBrush); painter.drawRoundedRect(QRectF(option.rect).adjusted(3,3,-3,-3),2,2); painter.restore()

    def subControlRect(self, control, option, subcontrol, widget=None):
        rect = super().subControlRect(control, option, subcontrol, widget)
        if control == QStyle.CC_ComboBox and subcontrol == QStyle.SC_ComboBoxEditField and isinstance(widget, QComboBox):
            rect.setLeft(max(0, input_text_inset(widget)-2 if widget.isEditable() else input_text_inset(widget)))
        elif control == QStyle.CC_SpinBox and subcontrol == QStyle.SC_SpinBoxEditField and isinstance(widget, QAbstractSpinBox) and widget.alignment() & Qt.AlignLeft:
            rect.setLeft(max(0, input_text_inset(widget)-2))
        return rect


def ensure_control_style():
    app = QApplication.instance()
    if app is not None and not isinstance(app.style(), ControlBorderStyle):
        # Create a separate native base style, never transfer the live app style.
        app.setStyle(ControlBorderStyle(app.style().objectName()))
        app.setStyleSheet(app.styleSheet() + "\nQPushButton:enabled:hover:!pressed, QToolButton:enabled:hover:!pressed { background-color: #d2d5d9; } QMainWindow::separator { width: 1px; height: 1px; background: #969da6; } QDockWidget { border: 1px solid #969da6; }")
    if app is not None and not hasattr(app, '_input_inset_filter'):
        app._input_inset_filter = InputInsetFilter(app)
        app.installEventFilter(app._input_inset_filter)


class FixedMaskKey(QLineEdit):
    """Retain the actual key in the native editor; show a constant mask."""
    def __init__(self):
        super().__init__()
        self.setEchoMode(QLineEdit.NoEcho)
        self.textChanged.connect(self.update)

    def mask_text(self):
        return '●' * 12 if self.text() else ''

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self.text():
            return
        from PySide6.QtWidgets import QStyleOptionFrame
        option = QStyleOptionFrame()
        self.initStyleOption(option)
        rect = self.style().subElementRect(QStyle.SE_LineEditContents, option, self)
        margins = self.textMargins()
        rect.adjust(margins.left(),margins.top(),-margins.right(),-margins.bottom())
        rect.adjust(2, 0, -2, 0)
        painter = QPainter(self)
        painter.setClipRect(rect)
        group = QPalette.Active if self.isEnabled() else QPalette.Disabled
        painter.setPen(self.palette().color(group,QPalette.Text))
        painter.drawText(rect,Qt.AlignLeft|Qt.AlignVCenter,self.mask_text())


def _paint_detail_divider(widget):
    painter = QPainter(widget)
    painter.fillRect(widget.rect(),QColor('white'))
    painter.fillRect(0,(widget.height()-1)//2,widget.width(),1,QColor('#d6d6d6'))


class DetailDividerHandle(QSplitterHandle):
    def paintEvent(self, event):
        _paint_detail_divider(self)


class DetailSplitter(QSplitter):
    def createHandle(self):
        return DetailDividerHandle(self.orientation(),self)


class DetailHeightGrip(QWidget):
    """Resize the window while assigning its extra space to the detail pane."""
    def __init__(self, splitter, parent=None):
        super().__init__(parent)
        self.splitter = splitter
        self._drag = None
        self.setFixedHeight(5)
        self.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip('上下拖动调整详情和窗口高度，下方操作区保持高度；受屏幕范围限制。')

    def paintEvent(self, event):
        _paint_detail_divider(self)

    def enterEvent(self, event):
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.window().isMaximized():
            self._drag = (event.globalPosition().y(),self.window().height(),self.splitter.sizes())
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag is None:
            super().mouseMoveEvent(event)
            return
        start_y,height,sizes = self._drag
        window = self.window()
        available = window.screen().availableGeometry()
        minimum = window.minimumSizeHint().height()
        maximum = max(minimum,available.bottom()-window.frameGeometry().top()-
                      (window.frameGeometry().height()-window.height())+1)
        target = min(maximum,max(minimum,height+round(event.globalPosition().y()-start_y)))
        if self.splitter.minimumHeight() == self.splitter.maximumHeight():
            self.splitter.setFixedHeight(max(190, sum(sizes)+self.splitter.handleWidth()+target-height))
        window.resize(window.width(),target)
        window.layout().activate()
        if len(sizes)==2:
            self.splitter.setSizes([sizes[0],max(120,sizes[1]+window.height()-height)])
        event.accept()

    def mouseReleaseEvent(self, event):
        if event.button()==Qt.LeftButton and self._drag is not None:
            self._drag = None
            event.accept()
        else:
            super().mouseReleaseEvent(event)
