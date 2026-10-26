"""A single-owner console panel; redirected streams remain ordinary text logs."""

import os
import re
import shutil
import unicodedata


_ESCAPE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-_])')


def _clean(text):
    text = _ESCAPE.sub('', str(text)).replace('\r\n', '\n')
    return ''.join(c if c == '\n' or not unicodedata.category(c).startswith('C')
                   else '    ' if c == '\t' else '?' for c in text)


def _cells(text):
    return sum(0 if unicodedata.combining(c) or unicodedata.category(c) in ('Mn', 'Me')
               else 2 if unicodedata.east_asian_width(c) in ('W', 'F') else 1
               for c in text)


def _wrap(text, columns):
    result = []
    for logical in text.split('\n'):
        line, used = '', 0
        for char in logical:
            size = _cells(char)
            if used and used + size > max(1, columns - 1):
                result.append(line)
                line, used = '', 0
            line += char
            used += size
        result.append(line)
    return result


def _windows_vt(stream):
    """Enable VT on this stream's console handle and return its restoration hook."""
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    get_mode = kernel.GetConsoleMode
    get_mode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    get_mode.restype = wintypes.BOOL
    set_mode = kernel.SetConsoleMode
    set_mode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    set_mode.restype = wintypes.BOOL
    handle = msvcrt.get_osfhandle(stream.fileno())
    original = wintypes.DWORD()
    if not get_mode(handle, ctypes.byref(original)):
        return False, None
    if original.value & 4:
        return True, None
    if not set_mode(handle, original.value | 4):
        return False, None
    return True, lambda: set_mode(handle, original.value)


class ConsoleStatus:
    """Overwrite only the last panel. Call from one writer, on line boundaries.

    ``width`` accepts an integer or callable. ``enable_vt`` is a test override
    (bool or callable returning bool); it never overrides non-TTY detection.
    On resize, retain the previous panel once and establish a new anchor below
    it: classic Windows consoles clip while other terminals reflow old rows.
    External cursor movement and panels taller than the viewport are unsupported.
    """

    def __init__(self, stream, *, width=None, enable_vt=None):
        self.stream = stream
        self._width = width
        self._lines = []
        self._last_columns = None
        self._restore = None
        self._finished = False
        enabled = False
        try:
            if stream.isatty():
                if enable_vt is not None:
                    enabled = bool(enable_vt() if callable(enable_vt) else enable_vt)
                elif os.name == 'nt':
                    enabled, self._restore = _windows_vt(stream)
                else:
                    enabled = os.environ.get('TERM', '') != 'dumb'
        except (AttributeError, OSError, ValueError):
            enabled = False
        self.mode = 'ansi' if enabled else 'plain'

    def _columns(self):
        if self._width is not None:
            return max(2, int(self._width() if callable(self._width) else self._width))
        try:
            return max(2, os.get_terminal_size(self.stream.fileno()).columns)
        except (AttributeError, OSError, ValueError):
            return max(2, shutil.get_terminal_size((80, 24)).columns)

    def _clear(self, columns):
        if not self._lines:
            return
        if columns != self._last_columns:
            # Never guess upward across a resize: it could erase configuration.
            self._lines = []
            return
        rows = len(self._lines)
        self.stream.write('\r\x1b[%dA' % rows)
        for row in range(rows):
            self.stream.write('\x1b[2K')
            if row + 1 < rows:
                self.stream.write('\x1b[1B\r')
        if rows > 1:
            self.stream.write('\x1b[%dA' % (rows - 1))
        self.stream.write('\r')
        self._lines = []

    def render(self, text):
        if self._finished:
            return
        text = _clean(text).rstrip('\n')
        if self.mode == 'plain':
            self.stream.write(text + '\n')
        else:
            columns = self._columns()
            self._clear(columns)
            self._lines = _wrap(text, columns)
            self._last_columns = columns
            self.stream.write('\r' + '\r\n'.join(self._lines) + '\r\n')
        self.stream.flush()

    def notice(self, message):
        if self._finished:
            return
        if self.mode == 'ansi':
            columns = self._columns()
            self._clear(columns)
            self.stream.write('\r' + '\r\n'.join(_wrap(_clean(message).rstrip('\n'), columns)) + '\r\n')
        else:
            self.stream.write(_clean(message).rstrip('\n') + '\n')
        self.stream.flush()

    def finish(self):
        if self._finished:
            return
        self._finished = True
        try:
            self.stream.flush()
        finally:
            if self._restore is not None:
                self._restore()
                self._restore = None
