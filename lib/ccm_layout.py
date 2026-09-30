"""Parse, scale and remap tmux layout trees without trusting old pane IDs."""
import copy
import re

import ccm_snapshot_store as store


def parse(layout):
    store._layout_ids(layout)
    body = layout[5:]
    def node(pos):
        m = re.match(r'(\d+)x(\d+),(\d+),(\d+)', body[pos:])
        w, h, x, y = map(int, m.groups())
        pos += m.end()
        out = dict(w=w, h=h, x=x, y=y)
        if body[pos:pos+1] in ('{', '['):
            out['axis'] = body[pos]
            closing = '}' if body[pos] == '{' else ']'
            pos += 1
            out['children'] = []
            while True:
                child, pos = node(pos)
                out['children'].append(child)
                if body[pos] == closing:
                    pos += 1
                    break
                pos += 1
            children = out['children']
            if len(children) < 2:
                raise store.SnapshotError('Layout split needs at least two children')
            offset = x if out['axis'] == '{' else y
            for c in children:
                if out['axis'] == '{':
                    valid = c['x'] == offset and c['y'] == y and c['h'] == h
                    offset += c['w'] + 1
                else:
                    valid = c['y'] == offset and c['x'] == x and c['w'] == w
                    offset += c['h'] + 1
                if not valid:
                    raise store.SnapshotError('Layout children do not tile their parent')
            if offset - 1 != (x + w if out['axis'] == '{' else y + h):
                raise store.SnapshotError('Layout dimensions do not match its children')
        else:
            m = re.match(r',(\d+)', body[pos:])
            out['id'] = int(m[1])
            pos += m.end()
        return out, pos
    tree, _ = node(0)
    if tree['x'] or tree['y']:
        raise store.SnapshotError('Layout origin must be zero')
    return tree


def minimum(tree):
    if 'id' in tree:
        return 2, 2
    dims = [minimum(c) for c in tree['children']]
    if tree['axis'] == '{':
        return sum(w for w, h in dims) + len(dims) - 1, max(h for w, h in dims)
    return max(w for w, h in dims), sum(h for w, h in dims) + len(dims) - 1


def scale(tree, width, height):
    tree = copy.deepcopy(tree)
    mw, mh = minimum(tree)
    if width < mw or height < mh:
        raise store.SnapshotError(f'Terminal too small for saved splits (needs {mw}x{mh})')
    def fit(n, w, h, x, y):
        n.update(w=w, h=h, x=x, y=y)
        if 'id' in n:
            return
        axis = 0 if n['axis'] == '{' else 1
        key = 'w' if axis == 0 else 'h'
        children = n['children']
        available = (w if axis == 0 else h) - len(children) + 1
        weights = [c[key] for c in children]
        mins = [minimum(c)[axis] for c in children]
        sizes = [max(m, int(available * v / sum(weights))) for m, v in zip(mins, weights)]
        while sum(sizes) != available:
            if sum(sizes) < available:
                i = max(range(len(sizes)), key=lambda i: available * weights[i] / sum(weights) - sizes[i])
                sizes[i] += 1
            else:
                candidates = [i for i in range(len(sizes)) if sizes[i] > mins[i]]
                i = max(candidates, key=lambda i: sizes[i] - available * weights[i] / sum(weights))
                sizes[i] -= 1
        offset = x if axis == 0 else y
        for c, size in zip(children, sizes):
            fit(c, size if axis == 0 else w, h if axis == 0 else size,
                offset if axis == 0 else x, y if axis == 0 else offset)
            offset += size + 1
    fit(tree, width, height, 0, 0)
    return tree


def leaves(tree):
    if 'id' in tree:
        return [tree]
    return [leaf for c in tree['children'] for leaf in leaves(c)]


def rect(node):
    return [node[k] for k in ('x', 'y', 'w', 'h')]


def render(tree, pane_map):
    def node(n):
        body = f"{n['w']}x{n['h']},{n['x']},{n['y']}"
        if 'id' in n:
            return body + ',' + str(int(pane_map[n['id']].lstrip('%')))
        return body + n['axis'] + ','.join(node(c) for c in n['children']) + ('}' if n['axis'] == '{' else ']')
    body = node(tree)
    checksum = 0
    for byte in body.encode('ascii'):
        checksum = (((checksum >> 1) | ((checksum & 1) << 15)) + byte) & 0xffff
    return f'{checksum:04x},{body}'


def plan(tree, cwd_by_leaf):
    """Return idempotent split operations and leaf -> logical pane key."""
    ops, mapping = [], {}
    def walk(n, key):
        if 'id' in n:
            mapping[n['id']] = key
            return
        children = n['children']
        first = children[0]
        rest = copy.deepcopy(n)
        delta = first['w' if n['axis'] == '{' else 'h'] + 1
        coord, size = ('x', 'w') if n['axis'] == '{' else ('y', 'h')
        rest[coord] += delta
        rest[size] -= delta
        if len(children) == 2:
            rest = children[1]
        else:
            rest['children'] = children[1:]
        new_key = str(len(ops) + 1)
        ops.append({'key': key, 'new_key': new_key, 'axis': n['axis'],
                    'size': rest[size], 'old_rect': rect(first), 'new_rect': rect(rest),
                    'cwd': cwd_by_leaf[leaves(rest)[0]['id']]})
        walk(first, key)
        walk(rest, new_key)
    walk(tree, '0')
    return ops, mapping
