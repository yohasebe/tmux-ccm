"""Synthetic tmux listings for snapshot tests (no server or agent required)."""


def layout(body):
    checksum = 0
    for byte in body.encode('ascii'):
        checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xffff
    return f'{checksum:04x},{body}'


def inventory_query(listing):
    def query(command, *args, **kwargs):
        rows = []
        for idx, line in enumerate(listing.splitlines(), 1):
            _, _, name, cwd = line.split('\t')
            if command == 'list-windows':
                rows.append('\t'.join(('$1', f'@{idx}', str(idx), name, cwd,
                                      layout(f'80x24,0,0,{idx}'), '80', '24', '0', '1', 'IDLE', '', 'END')))
            elif command == 'list-panes':
                rows.append('\t'.join((f'@{idx}', f'%{idx}', '0', str(idx),
                                      'zsh', cwd, '', '1', '24', '', '', '0', 'END')))
            else:
                raise AssertionError(command)
        return '\n'.join(rows)
    return query
