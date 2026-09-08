"""Test external font changes and Qt database reload via a temporary font.

Run with the project's Python, then remove the target font externally. The
native Windows message, Qt signal, and read-only polling are logged separately.
Reload first reinitializes this process's fontconfig (Linux), then adds and
removes only a temporary application-font registration, then queries the
database to exercise Qt's lazy rebuild. The Qt add/remove path is an
implementation side effect, not a public refresh contract. No system fonts
are installed.

>>> callable(main)
True
"""

from __future__ import annotations

import argparse
import ctypes
from difflib import get_close_matches
import json
import sys
from datetime import datetime
from pathlib import Path
from time import perf_counter

from qtpy.QtCore import QByteArray, QAbstractNativeEventFilter, QTimer, qVersion
from qtpy.QtGui import QFont, QFontDatabase, QFontInfo, QFontMetricsF
from qtpy.QtWidgets import QApplication, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from ballontranslator.utils.font_refresh import FontconfigRefresh, reinitialize_current_fontconfig


class FontWatch(QWidget):
    """Display font changes and time explicit database reload experiments.

    >>> issubclass(FontWatch, QWidget)
    True
    """

    def __init__(self, app: QApplication, family: str, reload_font: Path, observe_only: bool = False) -> None:
        super().__init__()
        self.family = family
        self.observe_only = observe_only
        self.font_database = QFontDatabase if int(qVersion().split('.')[0]) >= 6 else QFontDatabase()
        self.signal_count = 0
        self.reloading = False
        self.reload_count = 0
        self.reload_font_data = QByteArray() if observe_only else QByteArray(reload_font.read_bytes())
        self.last_state: dict[str, object] | None = None
        self.setWindowTitle('Qt external font change experiment')
        self.resize(960, 600)
        self.output = QPlainTextEdit(self)
        self.output.setReadOnly(True)
        self.output.setMaximumBlockCount(3000)
        self.check_button = QPushButton('Query' if observe_only else 'Reload font database + query (wall time)', self)
        self.check_button.clicked.connect(self.check_now)
        layout = QVBoxLayout(self)
        layout.addWidget(self.output)
        layout.addWidget(self.check_button)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(150)
        self.refresh_timer.timeout.connect(self.check_after_signal)
        self.native_timer = QTimer(self)
        self.native_timer.setSingleShot(True)
        self.native_timer.setInterval(300)
        self.native_timer.timeout.connect(self.reload_after_windows_message)
        self.poll_timer = QTimer(self)
        self.poll_timer.setInterval(1000)
        self.poll_timer.timeout.connect(self.poll)
        app.fontDatabaseChanged.connect(self.font_database_changed)
        self.log(f'Qt {qVersion()}, platform={app.platformName()}, target={family}')
        if observe_only:
            self.log('Observation only: no fontconfig reinitialization or application font mutation.')
        else:
            self.log(f'Reload seed: {reload_font}; bytes read once before initial query.')
            self.log('Initial query/polling are read-only. Reload uses one owned temporary font ID.')
        self.sample('initial', force=True)
        self.poll_timer.start()

    def log(self, message: str) -> None:
        line = f'{datetime.now().isoformat(timespec="milliseconds")} {message}'
        self.output.appendPlainText(line)
        print(line, flush=True)

    def sample(self, reason: str, force: bool = False) -> float:
        started = perf_counter()
        families = self.font_database.families()
        font = QFont()
        font.setFamily(self.family)
        font.setPointSizeF(18)
        font.setWeight(QFont.Weight.Normal)
        info = QFontInfo(font)
        present = self.family in families
        state: dict[str, object] = {
            'present': present,
            'exact_match': info.exactMatch(),
            'resolved_family': info.family(),
            'styles': self.font_database.styles(self.family) if present else [],
            'W_width': QFontMetricsF(font).horizontalAdvance('W'),
            'family_count': len(families),
        }
        query_seconds = perf_counter() - started
        if force or state != self.last_state:
            self.log(f'{reason}: ' + json.dumps(state, ensure_ascii=False))
        if self.last_state is not None and self.last_state['present'] and not present:
            self.log(f'TARGET DISAPPEARED; Qt signals observed={self.signal_count}')
        if self.last_state is None and not present:
            candidates = get_close_matches(self.family, families, n=8, cutoff=0.45)
            self.log('Target absent at startup; possible names: ' + json.dumps(candidates, ensure_ascii=False))
        self.last_state = state
        return query_seconds

    def font_database_changed(self) -> None:
        self.signal_count += 1
        source = 'during explicit reload' if self.reloading else 'outside explicit reload'
        self.log(f'fontDatabaseChanged #{self.signal_count} ({source})')
        if self.reloading:
            return
        # Query after Qt finishes its native callback, merging repeated signals.
        self.refresh_timer.start()

    def check_after_signal(self) -> None:
        self.sample('after Qt signal', force=True)

    def check_now(self) -> None:
        if self.observe_only:
            self.sample('manual query', force=True)
        else:
            self.reload_and_query('manual')

    def windows_font_changed(self) -> None:
        self.log('Windows WM_FONTCHANGE received')
        if not self.reloading and not self.observe_only:
            # Leave the native callback before mutating Qt; coalesce broadcasts.
            self.native_timer.start()

    def reload_after_windows_message(self) -> None:
        self.reload_and_query('WM_FONTCHANGE')

    def reload_and_query(self, reason: str) -> None:
        """Invalidate via removal, then measure the first query's lazy rebuild.

        >>> callable(FontWatch.reload_and_query)
        True
        """
        if self.reloading or self.observe_only:
            return
        self.native_timer.stop()
        self.refresh_timer.stop()
        self.reloading = True
        self.reload_count += 1
        started = perf_counter()
        font_id = -1
        try:
            backend = QApplication.instance().platformName()
            reinitialized = (reinitialize_current_fontconfig() if backend in ('xcb', 'wayland', 'wayland-egl')
                             else FontconfigRefresh('skipped'))
            fontconfig_done = perf_counter()
            # Never remove all fonts: only this call's temporary registration.
            font_id = QFontDatabase.addApplicationFontFromData(self.reload_font_data)
            added = perf_counter()
            if font_id < 0:
                raise RuntimeError('Qt rejected reload seed; choose another --reload-font file')
            if not QFontDatabase.removeApplicationFont(font_id):
                raise RuntimeError(f'Qt could not remove temporary font ID {font_id}')
            font_id = -1
            removed = perf_counter()
            query_seconds = self.sample(f'after reload #{self.reload_count} ({reason})', force=True)
            finished = perf_counter()
            self.log('reload wall time (ms): ' + json.dumps({
                'fontconfig_reinit': round((fontconfig_done - started) * 1000, 3),
                'fontconfig_status': reinitialized.status,
                'add': round((added - fontconfig_done) * 1000, 3),
                'remove_invalidate': round((removed - added) * 1000, 3),
                'query_repopulate': round(query_seconds * 1000, 3),
                'total': round((finished - started) * 1000, 3),
            }))
        except Exception as error:
            self.log(f'RELOAD FAILED: {error}; elapsed_ms={(perf_counter() - started) * 1000:.3f}')
        finally:
            if font_id >= 0:
                self.log(f'Temporary font cleanup: {QFontDatabase.removeApplicationFont(font_id)}')
            self.reloading = False

    def poll(self) -> None:
        self.sample('poll change')


class WindowsFontMessages(QAbstractNativeEventFilter):
    """Observe WM_FONTCHANGE independently of Qt's database-change signal.

    >>> issubclass(WindowsFontMessages, QAbstractNativeEventFilter)
    True
    """

    def __init__(self, window: FontWatch) -> None:
        super().__init__()
        self.window = window

    def nativeEventFilter(self, event_type: object, message: object) -> tuple[bool, int]:
        if sys.platform == 'win32' and bytes(event_type) in (b'windows_generic_MSG', b'windows_dispatcher_MSG'):
            from ctypes.wintypes import MSG

            native = MSG.from_address(int(message))
            if native.message == 0x001D:
                self.window.windows_font_changed()
        return False, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--family', default='方正兰亭粗黑_GBK')
    parser.add_argument('--probe', action='store_true', help='Print initial state and exit without showing a window')
    parser.add_argument('--observe-only', action='store_true', help='Only observe native/Qt signals and query; never force refresh')
    parser.add_argument('--reload-font', type=Path, default=ROOT / 'ballontranslator/assets/font_refresh/Abel-Regular.ttf', help='Temporary seed font; defaults to bundled Abel')
    parser.add_argument('--font-engine', choices=('default', 'gdi'), default='default',
                        help='Qt font backend: default preserves platform settings; gdi requires Windows and Qt 6.8+')
    args = parser.parse_args()
    if args.font_engine == 'gdi':
        if sys.platform != 'win32':
            parser.error('--font-engine gdi is only available on Windows')
        if tuple(int(part) for part in qVersion().split('.')[:2]) < (6, 8):
            parser.error('--font-engine gdi requires Qt 6.8+')
    reload_font = args.reload_font
    if not args.observe_only and not reload_font.is_file():
        parser.error('Provide --reload-font with an existing readable TTF/OTF file')
    if not args.observe_only and tuple(int(part) for part in qVersion().split('.')[:2]) < (6, 4):
        parser.error('Forced reload requires Qt 6.4+; use --observe-only on older Qt')
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    qt_args = ['watch_font_database']
    if args.font_engine == 'gdi':
        qt_args += ['-platform', 'windows:fontengine=gdi']
    print(f'{datetime.now().isoformat(timespec="milliseconds")} Requested font engine: {args.font_engine}', flush=True)
    app = QApplication(qt_args)
    window = FontWatch(app, args.family, reload_font.resolve(), args.observe_only)
    if args.probe:
        return 0 if window.last_state and window.last_state['present'] else 1
    native_filter = WindowsFontMessages(window)
    if sys.platform == 'win32':
        app.installNativeEventFilter(native_filter)
    window.show()
    try:
        return app.exec()
    finally:
        if sys.platform == 'win32':
            app.removeNativeEventFilter(native_filter)


if __name__ == '__main__':
    raise SystemExit(main())