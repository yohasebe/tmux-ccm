"""Behavioral negative control: the old menu prompted and swallowed load errors."""
from unittest.mock import Mock

import dashboard


def test_checkpoint_menu_opens_picker_without_inline_cli(monkeypatch):
    d = object.__new__(dashboard.Dashboard)
    d.menu_items = [('Saved checkpoints', 'load')]
    d.menu_selected = 0
    d._prompt = Mock(return_value=None)
    d._do_snapshots = Mock()
    command = Mock(side_effect=AssertionError('CLI must run via the suspended terminal path'))
    monkeypatch.setattr(dashboard, 'cmd_snapshot_load', command)
    screen = Mock()
    d._handle_menu_key(10, screen)
    d._do_snapshots.assert_called_once_with(screen)
    d._prompt.assert_not_called()
    command.assert_not_called()
