"""`ccm status --json`: one JSON object per run, one record per tmux
window, and the window chosen by its id rather than by its directory."""
import json

import pytest

import ccm_core
import ccm_json
import ccm_pane_state
import ccm_render


def row(target, project, directory, window_id, completed="", activity=""):
    """One `list-windows` line in `_WINDOW_FORMAT` order."""
    return "\t".join([target, project, directory, "", completed, activity, "", "",
                      "", "", "", "", "", "", window_id])


@pytest.fixture
def tmux(monkeypatch):
    """Windows, panes and a recorder of which windows were detected."""
    world = {"rows": [], "panes": [], "detected": [], "prefetched": None}
    monkeypatch.setattr(ccm_core, "tmux_cmd",
                        lambda *a, **k: "\n".join(world["rows"])
                        if a[0] == "list-windows" else "")
    monkeypatch.setattr(ccm_core, "tmux_query",
                        lambda *a, **k: "\n".join(world["rows"])
                        if a[0] == "list-windows" else world.get("query"))
    monkeypatch.setattr(ccm_core, "ps_snapshot", lambda: "")
    monkeypatch.setattr(ccm_core, "_build_panes_cache", lambda: world["panes"])
    monkeypatch.setattr(ccm_core, "_force_mock_state", lambda: False)
    monkeypatch.setattr(ccm_core, "_resolve_window_state",
                        lambda r, *a: world["detected"].append(r["win_target"]) or "IDLE")
    monkeypatch.setattr(ccm_core, "read_cache_file", lambda *a: "")
    monkeypatch.setattr(ccm_core, "_resolve_ignored_panes", lambda *a: 0)
    monkeypatch.setattr(ccm_core, "_read_attention_markers", lambda *a: {})
    monkeypatch.setattr(ccm_core.ccm_detection, "find_claude_pid", lambda *a: None)
    monkeypatch.setattr(ccm_pane_state, "prefetch_visible",
                        lambda ids: world.__setitem__("prefetched", ids))
    monkeypatch.setattr(ccm_pane_state, "capture_pane_screen",
                        lambda pane: world.get("screens", {}).get(pane, []))
    monkeypatch.setattr(ccm_pane_state, "clear_prefetched", lambda: None)
    import ccm_roles
    monkeypatch.setattr(ccm_roles, "reconcile",
                        lambda pane, role, agent: world.setdefault("reconciled", []).append(pane))
    return world


class TestWindowRecords:
    def test_window_id_is_parsed(self):
        assert ccm_core._parse_window_line(row("s:1", "demo", "/d", "@7"))["window_id"] == "@7"

    def test_two_windows_on_one_directory(self, tmux):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "two", "/d", "@2")]
        assert [p.name for p in ccm_core.build_project_list()] == ["one"]
        assert sorted(p.window_id for p in ccm_core.build_project_list(by_window=True)) == ["@1", "@2"]

    def test_a_linked_window_counts_once(self, tmux):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("t:4", "one", "/d", "@1")]
        assert [p.window_id for p in ccm_core.build_project_list(by_window=True)] == ["@1"]

    def test_one_window_is_built_alone(self, tmux):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "two", "/e", "@2")]
        tmux["panes"] = [("s:1", "10", "%1", "codex", "1", "40", "", ""),
                         ("s:2", "20", "%2", "codex", "1", "40", "", "")]
        [p] = ccm_core.build_project_list(window_id="@2")
        assert p.name == "two"
        assert tmux["detected"] == ["s:2"]
        assert tmux["prefetched"] == ["%2"]
        assert [o.pane for o in p.sidekicks] == ["%2"]

    def test_one_window_reconciles_only_its_own_restore_roles(self, tmux):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "two", "/e", "@2"),
                        row("t:5", "two", "/e", "@2")]
        tmux["panes"] = [("s:1", "10", "%1", "codex", "1", "40", "", "role-a"),
                         ("s:2", "20", "%2", "codex", "1", "40", "", "role-b"),
                         ("t:5", "20", "%2", "codex", "1", "40", "", "role-b")]
        ccm_core.build_project_list(window_id="@2")
        assert tmux["reconciled"] == ["%2", "%2"]
        tmux["reconciled"] = []
        ccm_core.build_project_list()
        assert tmux["reconciled"] == ["%1", "%2", "%2"]

    def test_completed_at_as_stored(self, tmux):
        # Window activity moves the sort time, not the completion time.
        tmux["rows"] = [row("s:1", "one", "/d", "@1", "1700000000", "1800000000"),
                        row("s:2", "two", "/e", "@2", "", "1800000000")]
        built = {p.name: p for p in ccm_core.build_project_list(by_window=True)}
        assert built["one"].completed_raw == 1700000000
        assert built["two"].completed_raw is None


def run(monkeypatch, capsys, args):
    """(the JSON object printed, the exit code); a run that ends
    without one fails the test."""
    try:
        ccm_core.dispatch(["status"] + args)
        code = None
    except SystemExit as exit_:
        code = exit_.code
    except Exception as exc:  # the object is owed whatever happens
        code = repr(exc)
    out = capsys.readouterr().out
    assert out.count("\n") == 1, (code, out)
    return json.loads(out), code


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr(ccm_core, "hooks_configured", lambda: True)
    monkeypatch.setattr(ccm_render, "status_warnings", lambda projects: [])
    monkeypatch.setattr(ccm_json.ccm_spool, "spool_summary",
                        lambda: {"pending": 1, "held": 0, "expired": 2})


class TestStatusJson:
    def test_every_window(self, tmux, quiet, monkeypatch, capsys, tmp_path):
        tmux["rows"] = [row("s:1", "one", str(tmp_path), "@1"),
                        row("s:2", "two", str(tmp_path / "gone"), "@2")]
        tmux["panes"] = [("s:1", "10", "%5", "codex", "0", "40", "", "")]
        tmux["screens"] = {"%5": ["› Ask Codex to do anything",
                                  "  ← for agents · ? for shortcuts"]}
        out, code = run(monkeypatch, capsys, ["--json"])
        assert code == 0
        assert out["schema"] == 1
        assert out["hooks"] == {"configured": True}
        assert out["spool"] == {"pending": 1, "held": 0, "expired": 2}
        one, two = sorted(out["projects"], key=lambda p: p["name"])
        assert one["window_id"] == "@1"
        assert one["dir"] == str(tmp_path.resolve())
        assert one["sidekicks"] == [{"agent": "codex", "pane": "%5", "state": "IDLE",
                                     "state_source": "screen", "keys": "free"}]
        assert two["dir"] is None
        assert two["dir_raw"] == str(tmp_path / "gone")
        assert two["sidekicks"] == []

    def test_by_name(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "two", "/e", "@2")]
        out, code = run(monkeypatch, capsys, ["--json", "two"])
        assert (code, [p["window_id"] for p in out["projects"]]) == (0, ["@2"])

    def test_a_linked_window_is_not_ambiguous(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("t:3", "one", "/d", "@1")]
        out, code = run(monkeypatch, capsys, ["--json", "one"])
        assert (code, [p["window_id"] for p in out.get("projects", [])]) == (0, ["@1"])

    @pytest.mark.parametrize("rows, args, error", [
        ([row("s:1", "one", "/d", "@1"), row("s:2", "one", "/e", "@2")],
         ["--json", "one"], "ambiguous"),
        ([row("s:1", "one", "/d", "@1")], ["--json", "two"], "no_match"),
        ([], ["--json", "a", "b"], "usage"),
        ([], ["--json", "--all"], "usage"),
    ])
    def test_errors(self, tmux, quiet, monkeypatch, capsys, rows, args, error):
        tmux["rows"] = rows
        out, code = run(monkeypatch, capsys, args)
        assert (code, out.get("error", {}).get("code")) == (1, error)
        assert "projects" not in out

    @pytest.mark.parametrize("args", [["--json"], ["--json", "one"]])
    def test_one_listing_serves_selection_and_records(self, tmux, quiet, monkeypatch,
                                                      capsys, args):
        # A second listing could fail on its own and read as no windows.
        tmux["rows"] = [row("s:1", "one", "/d", "@1")]
        monkeypatch.setattr(ccm_core, "tmux_cmd", lambda *a, **k: "")
        out, code = run(monkeypatch, capsys, args)
        assert (code, [p["window_id"] for p in out.get("projects", [])]) == (0, ["@1"])

    def test_tmux_unavailable(self, tmux, quiet, monkeypatch, capsys):
        monkeypatch.setattr(ccm_core, "tmux_query", lambda *a, **k: None)
        out, code = run(monkeypatch, capsys, ["--json"])
        assert (code, out["error"]["code"]) == (1, "tmux_unavailable")

    def test_no_windows_is_an_empty_list(self, tmux, quiet, monkeypatch, capsys):
        out, code = run(monkeypatch, capsys, ["--json"])
        assert (code, out["projects"]) == (0, [])

    def test_help(self, tmux, monkeypatch, capsys):
        out, code = run(monkeypatch, capsys, ["--json", "--help"])
        assert code == 0
        assert out["help"].startswith("Usage: ccm status")

    def test_self_from_the_caller_pane(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "one", "/d", "@2")]
        tmux["query"] = "@2"
        monkeypatch.setattr(ccm_core, "caller_context", lambda **k: ("one", "%9"))
        out, code = run(monkeypatch, capsys, ["--json", "self"])
        assert (code, [p["window_id"] for p in out["projects"]]) == (0, ["@2"])

    def test_self_from_the_directory(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1"), row("s:2", "two", "/e", "@2")]
        monkeypatch.setattr(ccm_core, "caller_context", lambda **k: ("two", ""))
        monkeypatch.setattr(ccm_core, "unique_registered_window",
                            lambda cwd: ("@2", "two", "/e"))
        out, code = run(monkeypatch, capsys, ["--json", "self"])
        assert (code, [p["window_id"] for p in out["projects"]]) == (0, ["@2"])

    def test_self_in_a_removed_directory(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1")]
        monkeypatch.setattr(ccm_core, "caller_context", lambda **k: ("unknown", ""))

        def gone():
            raise FileNotFoundError("removed")
        monkeypatch.setattr(ccm_json.os, "getcwd", gone)
        out, code = run(monkeypatch, capsys, ["--json", "self"])
        assert (code, out.get("error", {}).get("code")) == (1, "self_unresolved")

    def test_self_unresolved(self, tmux, quiet, monkeypatch, capsys):
        tmux["rows"] = [row("s:1", "one", "/d", "@1")]
        monkeypatch.setattr(ccm_core, "caller_context", lambda **k: ("unknown", ""))
        monkeypatch.setattr(ccm_core, "unique_registered_window", lambda cwd: None)
        out, code = run(monkeypatch, capsys, ["--json", "self"])
        assert (code, out["error"]["code"]) == (1, "self_unresolved")


class TestStatusText:
    def test_plain_status_is_the_table(self, monkeypatch):
        called = []
        monkeypatch.setattr(ccm_render, "print_status", lambda: called.append(1))
        ccm_core.dispatch(["status"])
        assert called == [1]

    def test_other_arguments_without_json_are_refused(self, monkeypatch):
        monkeypatch.setattr(ccm_render, "print_status", lambda: pytest.fail("printed"))
        with pytest.raises(SystemExit):
            ccm_core.dispatch(["status", "one"])

    def test_text_and_json_share_the_warnings(self, monkeypatch, capsys):
        monkeypatch.setattr(ccm_render, "status_warnings", lambda projects: ["look here"])
        monkeypatch.setattr(ccm_core, "build_project_list",
                            lambda **k: [ccm_core.Project("s:1", "1", "one", "/d", "IDLE")])
        monkeypatch.setattr(ccm_core, "hooks_configured", lambda: True)
        monkeypatch.setattr(ccm_render.ccm_spool, "pending_counts", lambda: {})
        monkeypatch.setattr(ccm_render, "signal_age_suffix", lambda *a, **k: "")
        ccm_render.print_status()
        assert "⚠ look here" in capsys.readouterr().out
        assert ccm_json.status_payload([])["warnings"] == ["look here"]
