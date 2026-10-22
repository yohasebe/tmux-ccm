"""A test that replaces `module.name` changes what that module sees only
if the module looks the name up there. A name that is imported but no
longer read (its last use moved to another module) can still be
patched without error, and the patch then does nothing: the test keeps
passing while the code it meant to fence off runs for real.

This walks every test for patches of a lib module's own attribute and
checks that the module defines the name, reads it as a module global
(a builtin counts), or that some lib module reads it as `module.name`.
A patch made with `raising=False` declares that the name may be absent
and is left alone.

It is a lint for patches nothing reads, not proof that a test patches
the path it exercises: a name read both in a module and in another one
a method moved to passes here while the moved method's own lookup goes
unpatched. Targets that are not literals (a variable holding the name,
`patch.multiple`) are not checked. A name bound more than one way in a
scope (imported, then assigned) resolves to nothing rather than to a
guess.
"""

import ast
import glob
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(HERE), "lib")
MODULES = {os.path.basename(p)[:-3]: p
           for p in glob.glob(os.path.join(LIB, "*.py"))}

_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)
_COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)
_PATCH = "<mock.patch>"     # what an import of mock's `patch` binds


def _import_bindings(node, modules):
    """`{bound name: what}` for an import statement: the lib module it
    names, `_PATCH` for mock's `patch`, else None."""
    out = {}
    if isinstance(node, ast.Import):
        for alias in node.names:
            top = alias.name.split(".")[0]
            target = alias.name if alias.asname else top
            out[alias.asname or top] = target if target in modules else None
    elif isinstance(node, ast.ImportFrom):
        for alias in node.names:
            out[alias.asname or alias.name] = (
                _PATCH if node.module in ("unittest.mock", "mock")
                and alias.name == "patch" else None)
    return out


class _Bindings:
    """The names one scope binds: what each import bound it to, and
    which names something other than an import binds."""

    def __init__(self):
        self.imported, self.assigned = {}, set()
        self.declared_global = set()

    def __contains__(self, name):
        return name in self.imported or name in self.assigned

    def resolve(self, name):
        """The module (or `_PATCH`) the name holds, or None when it is
        bound otherwise or more than one way."""
        if name in self.assigned:
            return None
        return self.imported.get(name)

    def defined(self):
        return set(self.assigned)


def _comprehension_targets(node):
    return [g.target for g in node.generators]


def scope_bindings(scope, modules):
    """`_Bindings` for a module, function, lambda, class or
    comprehension. Nested scopes are not entered; their own names
    are."""
    found, declared, globals_ = _Bindings(), set(), set()
    if isinstance(scope, _FUNCTIONS):
        args = scope.args
        for a in (args.posonlyargs + args.args + args.kwonlyargs
                  + [args.vararg, args.kwarg]):
            if a is not None:
                found.assigned.add(a.arg)
        body = [scope.body] if isinstance(scope, ast.Lambda) else scope.body
    elif isinstance(scope, _COMPREHENSIONS):
        body = _comprehension_targets(scope)
    else:
        body = scope.body
    todo = list(body)
    while todo:
        node = todo.pop()
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            declared.update(node.names)
            if isinstance(node, ast.Global):
                globals_.update(node.names)
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            found.assigned.add(node.name)
            continue
        if isinstance(node, (ast.Lambda,) + _COMPREHENSIONS):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx,
                                                     (ast.Store, ast.Del)):
            found.assigned.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            found.assigned.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for name, what in _import_bindings(node, modules).items():
                if name in found.imported and found.imported[name] != what:
                    found.assigned.add(name)      # bound two ways
                found.imported[name] = what
        todo.extend(ast.iter_child_nodes(node))
    for name in declared:
        found.imported.pop(name, None)
        found.assigned.discard(name)
    found.declared_global = globals_
    return found


class _Scoped(ast.NodeVisitor):
    """Walks a tree keeping the scopes in force, so a name can be
    looked up the way Python would look it up there."""

    def __init__(self, tree, modules):
        self.modules = modules
        self.module_scope = scope_bindings(tree, modules)
        self.stack = []          # [(is_class, _Bindings)]

    def lookup(self, name):
        """`(bound in an enclosing local scope, what it resolves to)`."""
        for depth, (is_class, bound) in enumerate(reversed(self.stack)):
            if is_class and depth:
                continue            # a class body does not enclose methods
            if name in bound.declared_global:
                break               # `global` skips every enclosing scope
            if name in bound:
                return True, bound.resolve(name)
        return False, self.module_scope.resolve(name)

    def _enter(self, node, is_class, parts):
        self.stack.append((is_class, scope_bindings(node, self.modules)))
        for part in parts:
            self.visit(part)
        self.stack.pop()

    def visit_FunctionDef(self, node):
        for expr in node.decorator_list + node.args.defaults + [
                d for d in node.args.kw_defaults if d is not None]:
            self.visit(expr)
        self._enter(node, False, node.body)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node):
        for expr in node.args.defaults + [
                d for d in node.args.kw_defaults if d is not None]:
            self.visit(expr)
        self._enter(node, False, [node.body])

    def visit_ClassDef(self, node):
        for expr in node.decorator_list + node.bases + node.keywords:
            self.visit(expr)
        self._enter(node, True, node.body)

    def _comprehension(self, node):
        # The first iterable is evaluated in the enclosing scope.
        first, *rest = node.generators
        self.visit(first.iter)
        parts = [first.target, *first.ifs]
        for gen in rest:
            parts += [gen.target, gen.iter, *gen.ifs]
        if isinstance(node, ast.DictComp):
            parts += [node.key, node.value]
        else:
            parts.append(node.elt)
        self._enter(node, False, parts)

    visit_ListComp = visit_SetComp = visit_DictComp = _comprehension
    visit_GeneratorExp = _comprehension


class _Reads(_Scoped):
    """What a lib module reads: names looked up as its globals, and
    `module.attr` loads on a lib module however it was imported."""

    def __init__(self, tree, modules):
        super().__init__(tree, modules)
        self.names, self.attrs = set(), set()
        self.visit(tree)

    def visit_Name(self, node):
        if isinstance(node.ctx, ast.Load) and not self.lookup(node.id)[0]:
            self.names.add(node.id)

    def visit_Attribute(self, node):
        if isinstance(node.ctx, ast.Load) and isinstance(node.value, ast.Name):
            what = self.lookup(node.value.id)[1]
            if what in self.modules:
                self.attrs.add((what, node.attr))
        self.generic_visit(node)


def module_facts(sources):
    """From `{module: source}`: per module the names it defines (any
    binding but an import, at top level or by `global`) and the names
    it reads as globals, plus every `module.attr` read anywhere."""
    defined, read, attr_reads = {}, {}, set()
    modules = set(sources)
    for mod, src in sources.items():
        tree = ast.parse(src)
        names = scope_bindings(tree, modules).defined()
        names.update(n for node in ast.walk(tree)
                     if isinstance(node, ast.Global) for n in node.names)
        reads = _Reads(tree, modules)
        defined[mod], read[mod] = names, reads.names
        attr_reads |= reads.attrs
    return defined, read, attr_reads


class _Patches(_Scoped):
    """The patches of a lib module's own attribute in one test file."""

    def __init__(self, tree, modules, rel):
        super().__init__(tree, modules)
        self.rel, self.sites = rel, []
        self.visit(tree)

    def _dotted(self, value):
        if not (isinstance(value, ast.Constant)
                and isinstance(value.value, str)):
            return None
        parts = value.value.split(".")
        if (len(parts) == 2 and parts[0] in self.modules
                and parts[1].isidentifier()):
            return parts[0], parts[1]
        return None

    def _module_and_name(self, args):
        if (len(args) >= 2 and isinstance(args[0], ast.Name)
                and isinstance(args[1], ast.Constant)
                and isinstance(args[1].value, str)):
            what = self.lookup(args[0].id)[1]
            if what in self.modules:
                return what, args[1].value
        return None

    def _is_patch(self, func):
        if isinstance(func, ast.Name):
            return self.lookup(func.id)[1] == _PATCH
        return isinstance(func, ast.Attribute) and func.attr == "patch"

    def visit_Call(self, node):
        func = node.func
        dormant = any(k.arg == "raising" and isinstance(k.value, ast.Constant)
                      and k.value.value is False for k in node.keywords)
        target = None
        if self._is_patch(func):
            target = node.args and self._dotted(node.args[0])
        elif (isinstance(func, ast.Attribute) and func.attr == "object"
              and self._is_patch(func.value)):
            target = self._module_and_name(node.args)
        elif ((isinstance(func, ast.Attribute) and func.attr == "setattr")
              or (isinstance(func, ast.Name) and func.id == "setattr"
                  and not self.lookup("setattr")[0])):
            target = ((node.args and self._dotted(node.args[0]))
                      or self._module_and_name(node.args))
        if target and not dormant:
            self.sites.append((*target, f"{self.rel}:{node.lineno}"))
        self.generic_visit(node)

    def visit_Tuple(self, node):
        # ("module.name", lambda ...) pairs fed to setattr in a loop
        if (len(node.elts) == 2 and isinstance(node.elts[1], ast.Lambda)
                and self._dotted(node.elts[0])):
            self.sites.append((*self._dotted(node.elts[0]),
                               f"{self.rel}:{node.lineno}"))
        self.generic_visit(node)


def patch_sites(sources, modules):
    """Every `(module, name, where)` the test `sources` (`{file: source}`)
    patch on a lib module's own attribute."""
    sites = []
    for rel, src in sources.items():
        sites += _Patches(ast.parse(src), modules, rel).sites
    return sites


def ineffective(sites, facts):
    defined, read, attr_reads = facts
    return sorted(f"{where}: {mod}.{name}" for mod, name, where in sites
                  if name not in defined[mod] and name not in read[mod]
                  and (mod, name) not in attr_reads)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _repo_sites():
    tests = {os.path.relpath(p, HERE): _read(p)
             for p in sorted(glob.glob(os.path.join(HERE, "**", "*.py"),
                                       recursive=True))}
    return patch_sites(tests, set(MODULES))


def test_the_scan_finds_the_patches():
    """A scan that silently stopped finding patches would pass the
    check below with nothing checked."""
    assert len(_repo_sites()) > 1500


def test_every_patched_name_is_looked_up_where_it_is_patched():
    facts = module_facts({m: _read(p) for m, p in MODULES.items()})
    assert ineffective(_repo_sites(), facts) == []


# ─── what the scan counts ───

LIB_MODS = {"dash", "core"}


@pytest.mark.parametrize("source, expected", [
    ('from unittest.mock import patch\npatch("dash.tmux_cmd")',
     [("dash", "tmux_cmd")]),
    ('patch("dash.tmux_cmd")', []),     # not the mock patch
    ('from unittest.mock import patch as p\np("dash.x")', [("dash", "x")]),
    ('mock.patch("dash.x")', [("dash", "x")]),
    ('monkeypatch.setattr("dash.time", t)', [("dash", "time")]),
    ('import dash\nmonkeypatch.setattr(dash, "SPOOL", 1)', [("dash", "SPOOL")]),
    ('import dash as d\nmonkeypatch.setattr(d, "x", 1)', [("dash", "x")]),
    ('from unittest.mock import patch\nimport dash as d\npatch.object(d, "x")',
     [("dash", "x")]),
    ('def t():\n    import dash as d\n    setattr(d, "x", 1)', [("dash", "x")]),
    ('pairs = (("dash.get_session", lambda: "0"),)', [("dash", "get_session")]),
    ('from unittest.mock import patch\n@patch("dash.x")\ndef t(m): pass',
     [("dash", "x")]),
    ('patch("dash.time.time")', []),
    ('import dash\nmonkeypatch.setattr(dash, "x", 1, raising=False)', []),
    ('patch("os.path.exists")', []),
    ('thing.object(dash, "x")', []),
    ('mp.setattr(other, "x", 1)', []),
    ('from unittest.mock import patch\ndef t(patch):\n    patch("dash.x")', []),
])
def test_what_counts_as_a_patch(source, expected):
    assert [(m, n) for m, n, _ in
            patch_sites({"t.py": source}, LIB_MODS)] == expected


def test_a_global_declaration_skips_the_enclosing_function():
    source = ("import dash as d\n"
              "def outer():\n    import core as d\n"
              "    def inner():\n        global d\n"
              "        monkeypatch.setattr(d, 'x', 1)\n"
              "        def deeper():\n"
              "            monkeypatch.setattr(d, 'y', 1)\n")
    assert [(m, n) for m, n, _ in patch_sites({"t.py": source}, LIB_MODS)] \
        == [("dash", "x"), ("dash", "y")]


def test_a_nonlocal_declaration_reaches_the_enclosing_function():
    source = ("import dash as d\n"
              "def outer():\n    import core as d\n"
              "    def inner():\n        nonlocal d\n"
              "        monkeypatch.setattr(d, 'x', 1)\n")
    assert [(m, n) for m, n, _ in patch_sites({"t.py": source}, LIB_MODS)] \
        == [("core", "x")]


def test_an_alias_is_resolved_in_the_scope_of_the_patch():
    source = ("def first():\n    import dash as m\n"
              "    monkeypatch.setattr(m, 'a', 1)\n"
              "def second():\n    import core as m\n"
              "    monkeypatch.setattr(m, 'b', 1)\n")
    assert [(m, n) for m, n, _ in patch_sites({"t.py": source}, LIB_MODS)] \
        == [("dash", "a"), ("core", "b")]


# ─── what counts as the module looking the name up ───

def _check(lib_source, patched="dash.read_cache_file", other=""):
    facts = module_facts({"dash": lib_source, "core": other})
    mod, name = patched.split(".")
    return ineffective([(mod, name, "t.py:1")], facts)


@pytest.mark.parametrize("lib_source", [
    "from core import read_cache_file\ndef f():\n    return read_cache_file()",
    "from core import read_cache_file\nX = read_cache_file()",
    "from core import read_cache_file\nclass C:\n    def m(self):\n        return read_cache_file()",
    "from core import read_cache_file\ndef f():\n    return [read_cache_file() for _ in ()]",
    "def read_cache_file():\n    pass",
    "read_cache_file = None",
    "def f():\n    global read_cache_file\n    read_cache_file = 1",
    "from core import read_cache_file\nread_cache_file = None",
    "from core import read_cache_file\ndef f():\n    return [read_cache_file for read_cache_file in read_cache_file()]",
])
def test_a_global_lookup_makes_the_patch_effective(lib_source):
    assert _check(lib_source) == []


@pytest.mark.parametrize("lib_source", [
    "from core import read_cache_file",
    "from core import read_cache_file\ndef f(read_cache_file):\n    return read_cache_file()",
    "from core import read_cache_file\ndef f():\n    read_cache_file = g\n    return read_cache_file()",
    "from core import read_cache_file\ndef f():\n    def g():\n        return read_cache_file()\n    read_cache_file = 1",
    "from core import read_cache_file\nf = lambda read_cache_file: read_cache_file()",
    "from core import read_cache_file\ndef f():\n    return [read_cache_file for read_cache_file in ()]",
    "from core import read_cache_file\ndef f():\n    for read_cache_file in ():\n        read_cache_file()",
])
def test_a_local_of_the_same_name_is_not_a_lookup(lib_source):
    assert _check(lib_source) == ["t.py:1: dash.read_cache_file"]


@pytest.mark.parametrize("other, expected", [
    ("import dash\ndef f():\n    return dash.read_cache_file()", []),
    ("import dash as d\ndef f():\n    return d.read_cache_file()", []),
    ("def f():\n    import dash as d\n    return d.read_cache_file()", []),
    ("import dash\ndash.read_cache_file = None",
     ["t.py:1: dash.read_cache_file"]),
    ("import dash\ndef f(dash):\n    return dash.read_cache_file()",
     ["t.py:1: dash.read_cache_file"]),
    ("dash = object()\ndef f():\n    return dash.read_cache_file()",
     ["t.py:1: dash.read_cache_file"]),
    ("import dash as d\nd = object()\ndef f():\n    return d.read_cache_file()",
     ["t.py:1: dash.read_cache_file"]),
    ("import dash as d\ndef outer():\n    d = object()\n"
     "    def inner():\n        global d\n        return d.read_cache_file()", []),
    ("import dash as d\ndef outer():\n    d = object()\n"
     "    def inner():\n        global d\n"
     "        def deeper():\n            return d.read_cache_file()", []),
    ("import dash as d\ndef outer():\n    d = object()\n"
     "    def inner():\n        nonlocal d\n        return d.read_cache_file()",
     ["t.py:1: dash.read_cache_file"]),
])
def test_an_attribute_read_counts_only_on_the_module(other, expected):
    lib = "from core import read_cache_file"
    assert _check(lib, other=other) == expected
