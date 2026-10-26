"""Exercise real cursor/erase output without touching a console or camera."""

import io
import re
import unittest
from unittest.mock import patch

from console_status import ConsoleStatus, _cells


class Terminal(io.StringIO):
    def __init__(self, columns=40):
        super().__init__()
        self.columns = columns
        self.rows = [[]]
        self.row = self.col = 0

    def isatty(self):
        return True

    def write(self, text):
        super().write(text)
        tokens = re.findall(r'\x1b\[[0-9]*[ABK]|[^\x1b]', text)
        for token in tokens:
            if token.startswith('\x1b['):
                count = int(token[2:-1] or '1')
                if token.endswith('A'):
                    self.row = max(0, self.row - count)
                elif token.endswith('B'):
                    self.row += count
                else:
                    self.rows[self.row] = []
            elif token == '\r':
                self.col = 0
            elif token == '\n':
                self.row += 1
            else:
                size = _cells(token)
                if size and self.col + size > self.columns:
                    self.row += 1
                    self.col = 0
                while len(self.rows) <= self.row:
                    self.rows.append([])
                line = self.rows[self.row]
                while len(line) < self.col + size:
                    line.append(' ')
                if size:
                    line[self.col] = token
                    if size == 2:
                        line[self.col + 1] = ''
                elif self.col:
                    line[self.col - 1] += token
                self.col += size
            while len(self.rows) <= self.row:
                self.rows.append([])
        return len(text)

    def visible(self):
        return [''.join(row).rstrip() for row in self.rows]


class ConsoleStatusTests(unittest.TestCase):
    def panel(self, columns=40):
        terminal = Terminal(columns)
        terminal.write('HEADER\r\n')
        panel = ConsoleStatus(terminal, width=lambda: terminal.columns, enable_vt=True)
        return terminal, panel

    def test_updates_overwrite_only_previous_block(self):
        terminal, panel = self.panel()
        for index in range(20):
            panel.render('recording %d\nsecond line' % index)
        panel.finish()
        self.assertEqual(terminal.visible(), ['HEADER', 'recording 19', 'second line', ''])
        self.assertEqual((terminal.row, terminal.col), (3, 0))
        self.assertNotIn('\x1b[?25', terminal.getvalue())
        self.assertNotIn('\x1b[2J', terminal.getvalue())

    def test_long_to_short_erases_stale_lines_and_columns(self):
        terminal, panel = self.panel()
        panel.render('old text is long\nold second\nold third')
        panel.render('new')
        self.assertEqual(terminal.visible(), ['HEADER', 'new', '', '', ''])
        self.assertEqual((terminal.row, terminal.col), (2, 0))

    def test_chinese_combining_and_narrow_width(self):
        terminal, panel = self.panel(8)
        text = '小鼠录制状态 e\u0301 123456789'
        panel.render(text)
        self.assertTrue(all(_cells(line) <= 7 for line in panel._lines))
        self.assertEqual(''.join(panel._lines), text)
        self.assertTrue(any('e\u0301' in line for line in panel._lines))
        panel.render('完成')
        self.assertEqual(terminal.visible()[0:2], ['HEADER', '完成'])
        self.assertFalse(any('鼠' in line for line in terminal.visible()))

    def test_resize_establishes_safe_anchor_once(self):
        terminal, panel = self.panel(30)
        panel.render('old wide status line')
        # Classic conhost may clip instead of reflowing the existing rows.
        terminal.columns = 8
        offset = len(terminal.getvalue())
        panel.render('新的状态很长')
        self.assertNotIn('A', terminal.getvalue()[offset:])
        panel.render('完成')
        self.assertEqual(terminal.visible()[0:3], ['HEADER', 'old wide status line', '完成'])
        terminal.columns = 50
        panel.render('wide again')
        panel.render('last')
        self.assertEqual(terminal.visible()[0], 'HEADER')
        self.assertIn('last', terminal.visible())

    def test_notice_is_permanent_and_next_panel_updates(self):
        terminal, panel = self.panel()
        panel.render('old\nsecond')
        panel.notice('encoder warning')
        panel.render('current')
        panel.render('final')
        self.assertEqual(terminal.visible()[0:4], ['HEADER', 'encoder warning', 'final', ''])

    def test_redirected_stream_never_uses_ansi_even_with_override(self):
        stream = io.StringIO()
        panel = ConsoleStatus(stream, enable_vt=True)
        panel.render('one')
        panel.render('two')
        panel.notice('warning')
        panel.finish()
        self.assertEqual(panel.mode, 'plain')
        self.assertEqual(stream.getvalue(), 'one\ntwo\nwarning\n')

    def test_untrusted_controls_are_sanitized(self):
        terminal, panel = self.panel()
        panel.render('safe\x1b[2J\r\b\x00\x1b]0;title\x07')
        self.assertEqual(terminal.visible()[0:2], ['HEADER', 'safe???'])
        self.assertNotIn('\x1b[2J', terminal.getvalue())

    def test_windows_vt_failure_falls_back(self):
        terminal = Terminal()
        with patch('console_status.os.name', 'nt'), patch('console_status._windows_vt', return_value=(False, None)) as enable:
            panel = ConsoleStatus(terminal)
            panel.render('plain')
        enable.assert_called_once_with(terminal)
        self.assertEqual(panel.mode, 'plain')
        self.assertNotIn('\x1b', terminal.getvalue())

    def test_original_windows_mode_restored_once(self):
        terminal = Terminal()
        restored = []
        with patch('console_status.os.name', 'nt'), patch('console_status._windows_vt', return_value=(True, lambda: restored.append(True))):
            panel = ConsoleStatus(terminal, width=40)
            panel.render('final')
            panel.finish()
            panel.finish()
        self.assertEqual(restored, [True])
        self.assertEqual(terminal.visible(), ['final', ''])


if __name__ == '__main__':
    unittest.main()
