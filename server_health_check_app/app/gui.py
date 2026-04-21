from __future__ import annotations

import sys
from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QAction, QDesktopServices, QTextOption
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTextEdit,
    QVBoxLayout,
    QWidget,
    QCheckBox,
)

from .checks import AppConfig, parallel_checks
from .models import CheckResult, STATUS_META, StepState
from .network_diagnostics import TARGET_IP, diagnose_connectivity


STEP_TITLES = {
    1: "Network Connection Verification",
    2: "Service Check",
    3: "Docker Check",
    4: "Main Web UI Check",
    5: "Port 5000 Secure UI Check",
    6: "Log Download",
}


class WorkerSignals(QObject):
    step_started = Signal(int)
    result_ready = Signal(object)
    workflow_error = Signal(str)
    workflow_done = Signal()


class HealthCheckWorker(QRunnable):
    def __init__(self, config: AppConfig):
        super().__init__()
        self.config = config
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            self.signals.step_started.emit(1)
            step1 = diagnose_connectivity(self.config.host)
            self.signals.result_ready.emit(step1)

            if step1.state == StepState.FAILED:
                for step_id in range(2, 7):
                    blocked = CheckResult(
                        step_id=step_id,
                        title=STEP_TITLES[step_id],
                        state=StepState.BLOCKED,
                        summary="Blocked by connectivity issue.",
                        details=f"Step 1 failed with cause {step1.cause_code or 'unknown'}: {step1.summary}",
                        severity="Info",
                        suggestion="Resolve Step 1 first, then retry.",
                    )
                    self.signals.result_ready.emit(blocked)
                self.signals.workflow_done.emit()
                return

            for step_id in range(2, 7):
                self.signals.step_started.emit(step_id)

            for result in parallel_checks(self.config):
                self.signals.result_ready.emit(result)
            self.signals.workflow_done.emit()
        except Exception as exc:
            self.signals.workflow_error.emit(str(exc))


class StepCard(QGroupBox):
    def __init__(self, step_id: int, title: str):
        super().__init__(f"Step {step_id} - {title}")
        self.step_id = step_id
        self.status_label = QLabel()
        self.summary_label = QLabel()
        self.severity_label = QLabel()
        self.cause_label = QLabel()
        self.suggestion_label = QLabel()
        self.details_box = QTextEdit()
        self.details_box.setReadOnly(True)
        self.details_box.setMinimumHeight(110)
        self.details_box.setWordWrapMode(QTextOption.WrapAnywhere)

        layout = QVBoxLayout(self)
        meta_grid = QGridLayout()
        meta_grid.addWidget(QLabel("Status:"), 0, 0)
        meta_grid.addWidget(self.status_label, 0, 1)
        meta_grid.addWidget(QLabel("Severity:"), 1, 0)
        meta_grid.addWidget(self.severity_label, 1, 1)
        meta_grid.addWidget(QLabel("Cause:"), 2, 0)
        meta_grid.addWidget(self.cause_label, 2, 1)
        layout.addLayout(meta_grid)
        layout.addWidget(QLabel("Summary:"))
        layout.addWidget(self.summary_label)
        layout.addWidget(QLabel("Suggested Fix / Action:"))
        layout.addWidget(self.suggestion_label)
        layout.addWidget(QLabel("Details:"))
        layout.addWidget(self.details_box)
        self.update_result(
            CheckResult(
                step_id=step_id,
                title=title,
                state=StepState.IDLE,
                summary="Waiting to run.",
                details="",
                severity="Info",
                suggestion="",
            )
        )

    def set_running(self) -> None:
        self.update_result(
            CheckResult(
                step_id=self.step_id,
                title=self.title(),
                state=StepState.RUNNING,
                summary="Check is in progress.",
                details=self.details_box.toPlainText(),
                severity="Info",
                suggestion="Please wait for completion.",
            )
        )

    def update_result(self, result: CheckResult) -> None:
        status_text, color = STATUS_META[result.state]
        self.status_label.setText(status_text)
        self.status_label.setStyleSheet(f"font-weight: 700; color: {color};")
        self.summary_label.setText(result.summary)
        self.summary_label.setWordWrap(True)
        self.severity_label.setText(result.severity)
        self.cause_label.setText(result.cause_code or "-")
        self.suggestion_label.setText(result.suggestion or "-")
        self.suggestion_label.setWordWrap(True)
        self.details_box.setPlainText(result.details or "-")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Server First-Level Health Check")
        self.resize(1180, 820)
        self.thread_pool = QThreadPool.globalInstance()
        self.cards = {}
        self._build_ui()

    def _build_ui(self) -> None:
        central = QWidget()
        main_layout = QVBoxLayout(central)

        config_box = QGroupBox("Connection Settings")
        form = QFormLayout(config_box)

        self.host_input = QLineEdit(TARGET_IP)
        self.port_input = QLineEdit("22")
        self.user_input = QLineEdit()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.Password)
        self.key_input = QLineEdit()
        self.logs_remote_input = QLineEdit("/var/logs/")
        self.logs_local_input = QLineEdit(str(Path.home() / "server_health_logs"))
        self.verify_tls_checkbox = QCheckBox("Verify HTTPS certificate")

        browse_button = QPushButton("Browse…")
        browse_button.clicked.connect(self.select_key)
        browse_logs_button = QPushButton("Logs folder…")
        browse_logs_button.clicked.connect(self.select_log_dir)

        key_row = QHBoxLayout()
        key_row.addWidget(self.key_input)
        key_row.addWidget(browse_button)

        logs_row = QHBoxLayout()
        logs_row.addWidget(self.logs_local_input)
        logs_row.addWidget(browse_logs_button)

        form.addRow("Server IP:", self.host_input)
        form.addRow("SSH Port:", self.port_input)
        form.addRow("SSH Username:", self.user_input)
        form.addRow("SSH Password:", self.password_input)
        form.addRow("SSH Private Key:", self._wrap_layout(key_row))
        form.addRow("Remote Log Path:", self.logs_remote_input)
        form.addRow("Local Log Folder:", self._wrap_layout(logs_row))
        form.addRow("TLS:", self.verify_tls_checkbox)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("Run Health Check")
        self.run_button.clicked.connect(self.run_checks)
        self.retry_button = QPushButton("Retry")
        self.retry_button.clicked.connect(self.run_checks)
        self.open_logs_button = QPushButton("Open Local Logs Folder")
        self.open_logs_button.clicked.connect(self.open_logs_folder)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.retry_button)
        button_row.addWidget(self.open_logs_button)
        button_row.addStretch(1)

        self.banner = QLabel(
            "Step 1 is the root dependency. If connectivity fails, steps 2–6 will be marked as Blocked, not Failed."
        )
        self.banner.setStyleSheet("padding: 10px; background: #eef5ff; border: 1px solid #c7ddff; border-radius: 8px;")
        self.banner.setWordWrap(True)

        cards_container = QWidget()
        cards_layout = QGridLayout(cards_container)
        row = col = 0
        for step_id in range(1, 7):
            card = StepCard(step_id, STEP_TITLES[step_id])
            self.cards[step_id] = card
            cards_layout.addWidget(card, row, col)
            col += 1
            if col == 2:
                row += 1
                col = 0

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(cards_container)

        main_layout.addWidget(config_box)
        main_layout.addLayout(button_row)
        main_layout.addWidget(self.banner)
        main_layout.addWidget(scroll, 1)
        self.setCentralWidget(central)

    def _wrap_layout(self, layout):
        wrapper = QWidget()
        wrapper.setLayout(layout)
        return wrapper

    def select_key(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select SSH Private Key")
        if path:
            self.key_input.setText(path)

    def select_log_dir(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Select Local Log Folder")
        if path:
            self.logs_local_input.setText(path)

    def open_logs_folder(self) -> None:
        path = Path(self.logs_local_input.text().strip())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(path.as_uri())

    def build_config(self) -> AppConfig:
        return AppConfig(
            host=self.host_input.text().strip() or TARGET_IP,
            ssh_port=int(self.port_input.text().strip() or "22"),
            ssh_username=self.user_input.text().strip(),
            ssh_password=self.password_input.text(),
            ssh_key_path=self.key_input.text().strip(),
            logs_remote_path=self.logs_remote_input.text().strip() or "/var/logs/",
            logs_local_path=self.logs_local_input.text().strip() or str(Path.home() / "server_health_logs"),
            verify_tls=self.verify_tls_checkbox.isChecked(),
        )

    def reset_cards(self) -> None:
        for step_id, title in STEP_TITLES.items():
            self.cards[step_id].update_result(
                CheckResult(
                    step_id=step_id,
                    title=title,
                    state=StepState.IDLE,
                    summary="Waiting to run.",
                    details="",
                    severity="Info",
                    suggestion="",
                )
            )

    def run_checks(self) -> None:
        try:
            config = self.build_config()
        except ValueError:
            QMessageBox.warning(self, "Invalid configuration", "SSH Port must be a valid integer.")
            return

        self.reset_cards()
        self.run_button.setEnabled(False)
        self.retry_button.setEnabled(False)
        worker = HealthCheckWorker(config)
        worker.signals.step_started.connect(self.on_step_started)
        worker.signals.result_ready.connect(self.on_result_ready)
        worker.signals.workflow_error.connect(self.on_workflow_error)
        worker.signals.workflow_done.connect(self.on_workflow_done)
        self.thread_pool.start(worker)

    def on_step_started(self, step_id: int) -> None:
        self.cards[step_id].set_running()

    def on_result_ready(self, result: CheckResult) -> None:
        self.cards[result.step_id].update_result(result)
        if result.step_id == 1 and result.state == StepState.FAILED:
            self.banner.setText(
                f"Root connectivity problem detected: {result.summary}\n"
                f"Cause: {result.cause_code or '-'} | Severity: {result.severity}\n"
                f"Fix first: {result.suggestion}"
            )
            self.banner.setStyleSheet("padding: 10px; background: #fff1f0; border: 1px solid #ffccc7; border-radius: 8px;")
        elif result.step_id == 1:
            self.banner.setText(
                f"Connectivity root check completed: {STATUS_META[result.state][0]}\n"
                f"{result.summary}\nDNS is not required for direct IP communication."
            )
            self.banner.setStyleSheet("padding: 10px; background: #f6ffed; border: 1px solid #b7eb8f; border-radius: 8px;")

    def on_workflow_error(self, message: str) -> None:
        self.run_button.setEnabled(True)
        self.retry_button.setEnabled(True)
        QMessageBox.critical(self, "Workflow error", message)

    def on_workflow_done(self) -> None:
        self.run_button.setEnabled(True)
        self.retry_button.setEnabled(True)


def launch() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
