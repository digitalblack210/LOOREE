"""Run Turaco: python -m turaco"""

import logging
import os
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler


def _setup_logging(console: bool) -> None:
    from .config import log_dir

    log_dir().mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(log_dir() / "turaco.log", maxBytes=500_000, backupCount=2, encoding="utf-8")
    ]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )

    # Without these, an error on the Qt thread or a worker thread would vanish
    # when there is no console.
    def log_uncaught(exc_type, exc, traceback) -> None:
        logging.getLogger("turaco").critical("Uncaught exception", exc_info=(exc_type, exc, traceback))

    sys.excepthook = log_uncaught
    threading.excepthook = lambda args: log_uncaught(args.exc_type, args.exc_value, args.exc_traceback)


def main() -> None:
    # Under pythonw there is no console; libraries that print (download
    # progress bars, for one) need somewhere to write.
    console = sys.stderr is not None
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")

    from . import win32

    if not win32.acquire_single_instance("Local\\TuracoSingleInstance"):
        print("Turaco is already running.")
        return

    _setup_logging(console)

    from PySide6.QtWidgets import QApplication

    from .app import App

    qt_app = QApplication(sys.argv)
    qt_app.setQuitOnLastWindowClosed(False)
    signal.signal(signal.SIGINT, signal.SIG_DFL)  # let Ctrl+C end the Qt loop
    app = App(qt_app)  # noqa: F841 - must stay referenced while Qt runs
    sys.exit(qt_app.exec())


if __name__ == "__main__":
    main()
