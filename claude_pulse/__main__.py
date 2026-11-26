"""Entry point for ``python -m claude_pulse`` (and ``run.pyw``)."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox

from .activity import SessionMonitor
from .app import Controller
from .demo import apply_demo_data, demo_accounts
from .settings import AccountConfig, Settings, discover_accounts
from .store import UsageStore

LAUNCHER = Path(__file__).resolve().parent.parent / 'run.pyw'


def main(argv: list[str]) -> int:
    demo = '--demo' in argv
    force_island = '--island' in argv

    application = QApplication(argv)
    application.setApplicationName('Claude Pulse')
    application.setQuitOnLastWindowClosed(False)

    lock = QLockFile(str(Path(tempfile.gettempdir()) / ('claude-pulse-demo.lock' if demo else 'claude-pulse.lock')))
    lock.setStaleLockTime(0)
    if not lock.tryLock(100):
        return 0

    settings = Settings()
    accounts = [AccountConfig(path.name, path) for path in demo_accounts()] if demo else discover_accounts(settings)
    if not accounts:
        QMessageBox.warning(
            None, 'Claude Pulse',
            'No Claude login found.\n\nSign in once with Claude Code (run "claude auth login"), then start Claude Pulse again.',
        )
        return 1

    store = UsageStore(accounts, settings)
    activity = SessionMonitor(accounts)
    controller = Controller(store, settings, LAUNCHER, activity)
    if demo:
        apply_demo_data(store, activity=activity)
    else:
        store.start()

    controller.start()
    if force_island:
        controller.set_island_mode('always', persist=False)

    return application.exec()


if __name__ == '__main__':
    sys.exit(main(sys.argv))
