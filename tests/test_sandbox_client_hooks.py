"""Sandbox-local agent hooks (task 3.3).

The merge is a pure function, so it is tested against the fixtures in
`test_sandbox_client_hook_fixtures/` rather than a live sandbox — see that
directory's README for what is assumed and how to re-record it.

The property every test here circles is the same one: amux adds its own hooks
and touches nothing else. A bootstrap that quietly dropped a template's own
`PreToolUse` audit hook would be a worse failure than not installing hooks at
all, because nothing would report it.
"""

from __future__ import annotations

import json
import shlex
import tomllib
from pathlib import Path

import pytest

from amux import sandbox_bootstrap as sb
from amux import sandbox_hooks as sh
from test_sandbox_client_bootstrap import FakeOps

FIXTURES = Path(__file__).parent / "test_sandbox_client_hook_fixtures"
SHIM = "/usr/local/bin/amux"
CONFIG = "/root/.config/amux/context.json"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text()


def merged_claude(name: str | None) -> dict:
    existing = json.loads(fixture(name)) if name else None
    return sh.merge_claude_settings(existing, shim=SHIM, config_path=CONFIG)


def commands(document: dict, event: str) -> list[str]:
    return [
        hook["command"]
        for group in document["hooks"][event]
        for hook in group["hooks"]
    ]


# --- Claude: the amux hooks get installed ------------------------------------


def test_every_amux_event_kind_gets_a_claude_hook():
    document = merged_claude("claude_template.json")
    for event, kind in sh.CLAUDE_EVENT_KINDS.items():
        assert any(f"event emit {kind} " in c for c in commands(document, event))


def test_the_hook_command_names_the_shim_the_config_and_the_agent():
    document = merged_claude("claude_template.json")
    command = commands(document, "PreToolUse")[0]
    assert SHIM in command
    assert f"AMUX_CONTEXT_CONFIG={CONFIG}" in command
    assert command.endswith("event emit busy --agent claude")


def test_the_hook_command_is_a_single_safely_quoted_shell_word_list():
    document = merged_claude("claude_template.json")
    for event in sh.CLAUDE_EVENT_KINDS:
        for command in commands(document, event):
            assert shlex.split(command)  # parses as a shell command at all


def test_every_hook_carries_a_timeout_so_an_unreachable_host_cannot_stall_the_agent():
    document = merged_claude("claude_template.json")
    for event in sh.CLAUDE_EVENT_KINDS:
        for group in document["hooks"][event]:
            for hook in group["hooks"]:
                if sh.HOOK_MARKER in hook["command"]:
                    assert hook["timeout"] == sh.HOOK_TIMEOUT


def test_tool_events_get_a_match_all_matcher_and_prompt_submit_gets_none():
    document = merged_claude("claude_synthetic_empty.json")
    amux_group = document["hooks"]["PreToolUse"][-1]
    assert amux_group["matcher"] == ""
    assert "matcher" not in document["hooks"]["UserPromptSubmit"][-1]


# --- Claude: template settings survive ---------------------------------------


def test_settings_outside_hooks_are_untouched():
    before = json.loads(fixture("claude_template.json"))
    after = merged_claude("claude_template.json")
    assert {k: v for k, v in after.items() if k != "hooks"} == before
    assert before["defaultMode"] == "bypassPermissions"  # the image really ships this


def test_a_templates_own_hooks_are_preserved_exactly():
    before = json.loads(fixture("claude_synthetic_with_hooks.json"))
    after = merged_claude("claude_synthetic_with_hooks.json")
    for event, groups in before["hooks"].items():
        assert after["hooks"][event][: len(groups)] == groups


def test_amux_appends_its_own_group_rather_than_joining_someone_elses():
    """Joining a template group would inherit its matcher: the template's
    PreToolUse group matches only Bash, so a busy hook added there would fire
    for one tool instead of all of them."""
    after = merged_claude("claude_synthetic_with_hooks.json")
    groups = after["hooks"]["PreToolUse"]
    assert len(groups) == 2
    assert groups[0]["matcher"] == "Bash"
    assert len(groups[0]["hooks"]) == 1  # still only the template's own hook
    assert groups[1]["matcher"] == ""
    assert sh.HOOK_MARKER in groups[1]["hooks"][0]["command"]


def test_the_source_document_is_not_mutated():
    existing = json.loads(fixture("claude_synthetic_with_hooks.json"))
    snapshot = json.dumps(existing, sort_keys=True)
    sh.merge_claude_settings(existing, shim=SHIM, config_path=CONFIG)
    assert json.dumps(existing, sort_keys=True) == snapshot


def test_a_missing_settings_file_produces_a_valid_document_from_nothing():
    document = merged_claude(None)
    assert set(document) == {"hooks"}
    assert set(document["hooks"]) == set(sh.CLAUDE_EVENT_KINDS)


def test_merging_twice_does_not_stack_duplicate_hooks():
    once = merged_claude("claude_synthetic_with_hooks.json")
    twice = sh.merge_claude_settings(once, shim=SHIM, config_path=CONFIG)
    assert twice == once


def test_the_rendered_settings_are_valid_json():
    text = sh.render_claude_settings(merged_claude("claude_synthetic_with_hooks.json"))
    assert json.loads(text) == merged_claude("claude_synthetic_with_hooks.json")
    assert text.endswith("\n")


@pytest.mark.parametrize(
    "broken", [[], "string", {"hooks": []}, {"hooks": {"Stop": "not-a-list"}}]
)
def test_configuration_that_cannot_be_merged_safely_is_refused(broken):
    with pytest.raises(sh.HookMergeError):
        sh.merge_claude_settings(broken, shim=SHIM, config_path=CONFIG)  # type: ignore[arg-type]


# --- Codex: the real hook surface, at parity with Claude ---------------------


def merged_codex(name: str | None) -> dict:
    existing = json.loads(fixture(name)) if name else None
    return sh.merge_codex_hooks(existing, shim=SHIM, config_path=CONFIG)


def test_codex_reaches_every_amux_kind_through_its_hook_events():
    """Codex is not structurally weaker than Claude: `hooks.json` has the events
    for all four kinds, with PermissionRequest standing in for Notification."""
    document = merged_codex("codex_synthetic_hooks_empty.json")
    kinds = {
        command.rsplit("event emit ", 1)[1].split()[0]
        for event in sh.CODEX_EVENT_KINDS
        for command in commands(document, event)
    }
    assert kinds == {"busy", "stop", "notify", "exit"}


def test_codex_session_end_asks_for_the_timeout_codex_will_actually_grant():
    """Codex caps SessionEnd at 3s and warns on every run when asked for more
    ("clamping SessionEnd hook timeout to 3s"), observed on codex-cli 0.146.0 in
    the real image. Asking for 3 keeps that noise out of the agent's output."""
    document = merged_codex("codex_synthetic_hooks_empty.json")
    timeout_for = lambda e: document["hooks"][e][-1]["hooks"][0]["timeout"]  # noqa: E731
    assert timeout_for("SessionEnd") == 3
    assert timeout_for("PreToolUse") == sh.HOOK_TIMEOUT  # everything else unchanged


def test_claude_session_end_is_not_clamped_because_only_codex_caps_it():
    document = merged_claude("claude_template.json")
    assert document["hooks"]["SessionEnd"][-1]["hooks"][0]["timeout"] == sh.HOOK_TIMEOUT


def test_codex_has_no_notification_event_and_uses_permission_request():
    assert "Notification" not in sh.CODEX_EVENT_KINDS
    assert sh.CODEX_EVENT_KINDS["PermissionRequest"] == "notify"


def test_codex_hooks_name_the_codex_agent_not_claude():
    document = merged_codex("codex_synthetic_hooks_empty.json")
    for event in sh.CODEX_EVENT_KINDS:
        for command in commands(document, event):
            assert command.endswith("--agent codex")


def test_a_codex_templates_own_hooks_are_preserved():
    before = json.loads(fixture("codex_synthetic_hooks.json"))
    after = merged_codex("codex_synthetic_hooks.json")
    groups = after["hooks"]["PreToolUse"]
    assert groups[0] == before["hooks"]["PreToolUse"][0]
    assert groups[0]["hooks"][0]["statusMessage"] == "Auditing tool use"
    assert len(groups) == 2


def test_every_codex_event_carries_a_match_all_matcher():
    """Unlike Claude, the verified Codex config puts a matcher on every event."""
    document = merged_codex("codex_synthetic_hooks_empty.json")
    for event in sh.CODEX_EVENT_KINDS:
        assert document["hooks"][event][-1]["matcher"] == ""


def test_merging_codex_hooks_twice_is_a_no_op():
    once = merged_codex("codex_synthetic_hooks.json")
    assert sh.merge_codex_hooks(once, shim=SHIM, config_path=CONFIG) == once


# --- Codex: hooks.json is inert without the feature switch -------------------


def test_the_hooks_feature_is_enabled_under_features_not_at_top_level():
    text = sh.enable_codex_hooks(fixture("codex_template.toml"))
    document = tomllib.loads(text)
    assert document["features"]["hooks"] is True
    assert "hooks" not in {k for k, v in document.items() if not isinstance(v, dict)}


def test_a_features_table_is_appended_when_the_image_has_none():
    """A new table header is safe at the end of a file; a bare key would land in
    whichever table happens to be last. Uses the synthetic fixture: the real
    image config has no tables, so it cannot exercise this."""
    text = sh.enable_codex_hooks(fixture("codex_synthetic_with_tables.toml"))
    document = tomllib.loads(text)
    assert document["features"]["hooks"] is True
    assert document["projects"]["/work/repo"] == {"trust_level": "trusted"}
    assert "hooks" not in document["projects"]["/work/repo"]


def test_hooks_false_in_an_existing_features_table_is_switched_on():
    text = sh.enable_codex_hooks(fixture("codex_synthetic_hooks_off.toml"))
    document = tomllib.loads(text)
    assert document["features"]["hooks"] is True
    assert document["features"]["js_repl"] is False  # sibling survives
    assert document["projects"]["/work/repo"] == {"trust_level": "trusted"}


def test_a_replaced_feature_line_is_commented_not_deleted():
    text = sh.enable_codex_hooks(fixture("codex_synthetic_hooks_off.toml"))
    assert "# amux replaced this: hooks = false" in text


def test_an_already_enabled_config_is_left_untouched():
    before = fixture("codex_synthetic_hooks_on.toml")
    assert sh.enable_codex_hooks(before) == before
    assert sh.codex_hooks_enabled(before) is True


def test_enabling_hooks_on_an_empty_config_produces_valid_toml():
    text = sh.enable_codex_hooks("")
    assert tomllib.loads(text)["features"]["hooks"] is True


def test_invalid_toml_is_refused_rather_than_rewritten():
    with pytest.raises(sh.HookMergeError):
        sh.enable_codex_hooks('model = "x\n[features\n')


# --- Codex version detection -------------------------------------------------


@pytest.mark.parametrize(
    "output,expected",
    [
        ("codex-cli 0.146.0", (0, 146, 0)),
        ("codex-cli 0.200.1\n", (0, 200, 1)),
        ("1.2", (1, 2)),
        ("", None),
        ("codex-cli unknown", None),
    ],
)
def test_the_codex_version_is_parsed_from_its_own_output(output, expected):
    assert sh.parse_codex_version(output) == expected


def test_the_verified_version_counts_as_having_hooks():
    assert sh.codex_supports_hooks("codex-cli 0.146.0") is True


def test_an_older_codex_does_not_count_as_having_hooks():
    assert sh.codex_supports_hooks("codex-cli 0.100.0") is False


def test_an_unreadable_version_falls_back_rather_than_assuming_hooks():
    """Falling back still reports `stop` and says what is missing; assuming a
    hook surface that is not there would report nothing at all."""
    assert sh.codex_supports_hooks("") is False
    assert sh.codex_supports_hooks("codex-cli dev") is False


# --- Codex: the older single notify slot, kept as a fallback ------------------


def test_codex_notify_is_pointed_at_the_amux_dispatch_script():
    text, previous = sh.merge_codex_config(fixture("codex_template.toml"))
    assert tomllib.loads(text)["notify"] == [sh.CODEX_DISPATCH_PATH]
    assert previous is None


def test_a_new_notify_is_prepended_so_it_cannot_land_inside_a_table():
    """Appending would put the key inside whichever table is last in the file,
    which is a different setting entirely. Synthetic fixture: the real image
    config has no tables."""
    text, _ = sh.merge_codex_config(fixture("codex_synthetic_with_tables.toml"))
    document = tomllib.loads(text)
    assert document["notify"] == [sh.CODEX_DISPATCH_PATH]
    assert document["projects"]["/work/repo"] == {"trust_level": "trusted"}
    assert "notify" not in document["projects"]["/work/repo"]


def test_other_codex_settings_survive():
    before = tomllib.loads(fixture("codex_template.toml"))
    text, _ = sh.merge_codex_config(fixture("codex_template.toml"))
    after = tomllib.loads(text)
    for key, value in before.items():
        assert after[key] == value


def test_an_existing_notify_is_returned_so_it_can_be_chained():
    text, previous = sh.merge_codex_config(fixture("codex_synthetic_with_notify.toml"))
    assert previous == ["/opt/template/notify.sh", "turn-ended"]
    assert tomllib.loads(text)["notify"] == [sh.CODEX_DISPATCH_PATH]


def test_a_replaced_notify_is_commented_out_not_deleted():
    text, _ = sh.merge_codex_config(fixture("codex_synthetic_with_notify.toml"))
    assert '# notify = ["/opt/template/notify.sh", "turn-ended"]' in text
    assert tomllib.loads(text)["notify"] == [sh.CODEX_DISPATCH_PATH]


def test_a_multiline_notify_array_is_replaced_whole():
    text, previous = sh.merge_codex_config(
        fixture("codex_synthetic_multiline_notify.toml")
    )
    assert previous == ["/opt/template/notify.sh", "turn-ended", "--verbose"]
    document = tomllib.loads(text)
    assert document["notify"] == [sh.CODEX_DISPATCH_PATH]
    assert document["approval_policy"] == "never"  # the line after the array


def test_merging_codex_twice_is_a_no_op():
    once, _ = sh.merge_codex_config(fixture("codex_synthetic_with_notify.toml"))
    twice, previous = sh.merge_codex_config(once)
    assert twice == once
    assert previous is None


def test_a_commented_out_notify_is_not_mistaken_for_the_real_one():
    text, previous = sh.merge_codex_config('# notify = ["/old"]\nmodel = "x"\n')
    assert previous is None
    assert tomllib.loads(text)["notify"] == [sh.CODEX_DISPATCH_PATH]


def test_a_notify_inside_a_table_is_not_treated_as_the_top_level_one():
    text, previous = sh.merge_codex_config(
        'model = "x"\n\n[profiles.other]\nnotify = ["/inner"]\n'
    )
    assert previous is None
    document = tomllib.loads(text)
    assert document["notify"] == [sh.CODEX_DISPATCH_PATH]
    assert document["profiles"]["other"]["notify"] == ["/inner"]


def test_an_unterminated_notify_array_is_refused_rather_than_guessed():
    with pytest.raises(sh.HookMergeError):
        sh.merge_codex_config('notify = [\n  "/opt/x",\n')


def test_a_non_string_notify_is_refused():
    with pytest.raises(sh.HookMergeError):
        sh.merge_codex_config("notify = 42\n")


# --- the Codex dispatch script -----------------------------------------------


def test_the_dispatch_script_pipes_codexs_argument_into_the_shims_stdin():
    """Codex passes the notification JSON as $1; the shim reads hook payloads on
    stdin, so the script has to bridge the two."""
    script = sh.render_codex_dispatch(SHIM, CONFIG)
    assert script.startswith("#!/bin/sh")
    assert 'printf %s "${1:-}"' in script
    assert f"{SHIM} event emit stop --agent codex" in script
    assert f"AMUX_CONTEXT_CONFIG={CONFIG}" in script


def test_the_dispatch_script_never_fails_the_agents_turn():
    script = sh.render_codex_dispatch(SHIM, CONFIG)
    assert "|| true" in script


def test_the_dispatch_script_chains_the_notify_it_replaced():
    script = sh.render_codex_dispatch(
        SHIM, CONFIG, previous=["/opt/template/notify.sh", "turn-ended"]
    )
    assert script.rstrip().endswith(
        "exec /opt/template/notify.sh turn-ended \"$@\""
    )


def test_the_dispatch_script_quotes_a_chained_command_with_spaces():
    script = sh.render_codex_dispatch(
        SHIM, CONFIG, previous=["/opt/my notify.sh", "a b"]
    )
    assert "'/opt/my notify.sh' 'a b'" in script


def test_without_a_previous_notify_the_script_does_not_exec_anything():
    script = sh.render_codex_dispatch(SHIM, CONFIG)
    assert "event emit stop --agent codex" in script  # it is a real script
    assert "exec" not in script


# --- honest coverage ---------------------------------------------------------


def test_claude_covers_every_state_a_host_agent_reports():
    assert set(sh.state_coverage("claude")) == {"busy", "stop", "notify", "exit"}
    assert sh.missing_kinds("claude") == ()


def test_a_current_codex_covers_every_state_too():
    """Parity, not degradation: `hooks.json` supplies all four kinds."""
    assert set(sh.state_coverage("codex")) == {"busy", "stop", "notify", "exit"}
    assert sh.missing_kinds("codex") == ()


def test_only_a_codex_without_hooks_is_degraded_and_it_says_which_kinds():
    assert sh.state_coverage("codex", hooks_supported=False) == ("stop",)
    assert set(sh.missing_kinds("codex", hooks_supported=False)) == {
        "busy",
        "notify",
        "exit",
    }


def test_spawn_is_never_reported_missing_because_no_hook_supplies_it():
    """`spawn` is stamped by the host at grid creation, so an adapter cannot be
    missing it and reporting it as absent would be a false alarm."""
    assert "spawn" not in sh.missing_kinds("codex", hooks_supported=False)
    assert "spawn" in sh.ALL_KINDS
    assert "spawn" not in sh.HOOK_SUPPLIED_KINDS


def test_an_unsupported_agent_is_refused_by_name():
    with pytest.raises(sh.HookMergeError) as failure:
        sh.hooks_for("gemini")
    assert "gemini" in str(failure.value)
    assert "claude" in str(failure.value)


# --- the fixtures say honestly where they came from --------------------------


def test_both_hook_locations_are_recorded_rather_than_assumed():
    """Verified against real images on 2026-08-05, so preflight may now say the
    location was checked. If a future image moves one, re-record and set the flag
    back — `location_verified` is what stops us implying we looked when we did
    not."""
    for adapter in (sh.CLAUDE, sh.CODEX):
        assert adapter.paths_are_assumed is False
        assert adapter.settings_relpath and not adapter.settings_relpath.startswith("/")


def test_recorded_and_synthetic_fixtures_are_distinguishable_by_name():
    """A re-record must not "correct" an invented edge case into uselessness: the
    real Codex config has no TOML tables, so the table hazard only has a
    synthetic fixture."""
    names = {p.name for p in FIXTURES.iterdir() if p.name != "README.md"}
    recorded = {n for n in names if "_template" in n}
    synthetic = {n for n in names if "_synthetic_" in n}
    assert recorded == {"claude_template.json", "codex_template.toml"}
    assert synthetic and recorded | synthetic == names


def test_the_fixture_readme_records_what_was_observed_and_how_to_redo_it():
    readme = (FIXTURES / "README.md").read_text()
    for observed in ("2.1.221", "codex-cli 0.146.0", "/home/agent", "synthetic"):
        assert observed in readme
    for step in ("sbx create", "sbx exec", "sbx rm", "paths_are_assumed"):
        assert step in readme


def test_the_shim_reads_the_detail_codex_actually_sends():
    """Codex's end-of-turn payload has no `message`/`tool_name`/`reason`, so
    without this the one event a Codex sandbox can report arrives detail-less."""
    from amux import sandbox_client as sc

    source = open(sc.__file__ or "").read()
    assert "last-assistant-message" in source


# --- installing hooks into a sandbox -----------------------------------------


@pytest.fixture
def installed():
    return sb.Installed(shim_path=SHIM, config_path=CONFIG, home="/root")


def written(ops, path: str) -> str:
    return ops.copied[path].decode()


def test_installing_claude_hooks_writes_a_merged_settings_file(tmp_path, installed):
    ops = FakeOps()
    ops.files["/root/.claude/settings.json"] = fixture("claude_synthetic_with_hooks.json")
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)

    assert result.settings_path == "/root/.claude/settings.json"
    document = json.loads(written(ops, result.settings_path))
    assert document["model"] == "claude-opus-4-5"  # the synthetic fixture's own key
    assert len(document["hooks"]["PreToolUse"]) == 2  # template's plus ours
    assert result.missing_kinds == ()
    assert result.degraded is False


def test_installing_hooks_when_the_image_ships_no_settings_file(tmp_path, installed):
    ops = FakeOps()  # ops.files is empty: `cat` returns ""
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    document = json.loads(written(ops, result.settings_path))
    assert set(document["hooks"]) == set(sh.CLAUDE_EVENT_KINDS)


def test_the_settings_directory_is_created_before_the_file_is_copied(tmp_path, installed):
    ops = FakeOps()
    sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    assert ["mkdir", "-p", "/root/.claude"] in ops.execs


def codex_ops(version: str = "codex-cli 0.146.0", **files) -> FakeOps:
    ops = FakeOps()
    ops.versions["codex"] = version
    ops.files.update(files)
    return ops


def test_a_current_codex_gets_hooks_json_and_is_not_degraded(tmp_path, installed):
    ops = codex_ops(**{"/root/.codex/hooks.json": fixture("codex_synthetic_hooks.json")})
    result = sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)

    assert result.settings_path == "/root/.codex/hooks.json"
    assert result.mechanism == "hooks"
    assert result.degraded is False
    assert result.missing_kinds == ()
    assert result.agent_version == "codex-cli 0.146.0"
    document = json.loads(written(ops, result.settings_path))
    assert set(document["hooks"]) == set(sh.CODEX_EVENT_KINDS)
    assert len(document["hooks"]["PreToolUse"]) == 2  # template's plus ours


def test_installing_codex_hooks_also_switches_the_feature_on(tmp_path, installed):
    """hooks.json alone is inert."""
    ops = codex_ops(**{"/root/.codex/config.toml": fixture("codex_synthetic_hooks_off.toml")})
    sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)
    config = tomllib.loads(written(ops, "/root/.codex/config.toml"))
    assert config["features"]["hooks"] is True
    assert config["projects"]["/work/repo"] == {"trust_level": "trusted"}


def test_a_config_that_already_enables_hooks_is_not_rewritten(tmp_path, installed):
    ops = codex_ops(**{"/root/.codex/config.toml": fixture("codex_synthetic_hooks_on.toml")})
    result = sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)
    # install DID run - hooks.json was written - so the untouched config is a
    # decision, not a no-op that would satisfy this assertion for free.
    assert result.settings_path in ops.copied
    assert "/root/.codex/config.toml" not in ops.copied


def test_no_dispatch_script_is_installed_for_a_codex_that_has_hooks(tmp_path, installed):
    ops = codex_ops()
    result = sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)
    assert result.mechanism == "hooks" and result.settings_path in ops.copied
    assert sh.CODEX_DISPATCH_PATH not in ops.copied


def test_an_old_codex_falls_back_to_notify_and_is_reported_degraded(tmp_path, installed):
    ops = codex_ops(
        "codex-cli 0.100.0",
        **{"/root/.codex/config.toml": fixture("codex_synthetic_with_notify.toml")},
    )
    result = sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)

    assert result.mechanism == "notify"
    assert result.degraded is True
    assert set(result.missing_kinds) == {"busy", "notify", "exit"}
    config = tomllib.loads(written(ops, result.settings_path))
    assert config["notify"] == [sh.CODEX_DISPATCH_PATH]
    assert config["model"] == "gpt-5.6"
    dispatch = written(ops, sh.CODEX_DISPATCH_PATH)
    assert dispatch.startswith("#!/bin/sh")
    assert "/opt/template/notify.sh" in dispatch  # the replaced one still fires
    assert result.previous_notify == ("/opt/template/notify.sh", "turn-ended")
    assert ops.mode_set_for(sh.CODEX_DISPATCH_PATH) == "755"


def test_a_codex_whose_version_cannot_be_read_falls_back_rather_than_guessing(
    tmp_path, installed
):
    ops = codex_ops("")
    result = sb.install_hooks(ops, "codex", installed, staging_dir=tmp_path)
    assert result.mechanism == "notify"
    assert result.degraded is True


def test_claude_is_never_version_probed(tmp_path, installed):
    """Claude's hook surface is not conditional, so probing would be noise."""
    ops = FakeOps()
    sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    assert ops.execs, "no commands ran, so finding no --version proves nothing"
    assert ["mkdir", "-p", "/root/.claude"] in ops.execs
    assert not any("--version" in " ".join(argv) for argv in ops.execs)


def test_image_configuration_that_is_not_valid_json_is_refused(tmp_path, installed):
    """Overwriting an image file we cannot parse would destroy settings silently."""
    ops = FakeOps()
    ops.files["/root/.claude/settings.json"] = "{not json"
    with pytest.raises(sb.BootstrapError) as failure:
        sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    assert "settings.json" in str(failure.value)
    assert ops.copied == {}


def test_hook_installation_reports_whether_the_location_was_verified(tmp_path, installed):
    ops = FakeOps()
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    assert result.location_verified is not sh.CLAUDE.paths_are_assumed


def test_hook_files_are_delivered_as_files_not_as_shell_arguments(tmp_path, installed):
    """A heredoc or `echo` would put the whole document in the process table and
    make quoting a correctness problem."""
    ops = FakeOps()
    ops.files["/root/.claude/settings.json"] = fixture("claude_template.json")
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    # the document really was delivered, and as a file
    assert "bypassPermissions" in written(ops, result.settings_path)
    assert ops.execs
    # Naming the path is fine — reading it back, chowning and chmoding all do.
    # Carrying the document is not, and that is what a heredoc would do.
    for argv in ops.execs:
        assert "bypassPermissions" not in " ".join(argv)
        assert not any(">" in arg or "<<" in arg for arg in argv[3:])
    readers = [a for a in ops.execs if a[:2] == ["sh", "-lc"] and a[2].startswith("cat ")]
    assert len(readers) == 1  # the only command that touches the file's contents
    assert result.settings_path in readers[0][2]


def test_no_hook_staging_file_is_left_on_the_host(tmp_path, installed):
    staging = tmp_path / "hook-staging"  # its own dir: tmp_path also holds isolate_state's
    ops = FakeOps()
    sb.install_hooks(ops, "codex", installed, staging_dir=staging)
    assert list(staging.iterdir()) == []


def test_a_home_the_agent_actually_uses_is_where_hooks_land(tmp_path):
    ops = FakeOps(home="/home/agent")
    installed = sb.Installed(
        shim_path=SHIM, config_path="/home/agent/.config/amux/context.json",
        home="/home/agent",
    )
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    assert result.settings_path == "/home/agent/.claude/settings.json"


def test_installing_hooks_for_an_unsupported_agent_is_refused(tmp_path, installed):
    ops = FakeOps()
    with pytest.raises(sh.HookMergeError):
        sb.install_hooks(ops, "gemini", installed, staging_dir=tmp_path)
    assert ops.copies == []


def test_installed_hooks_point_at_the_delivered_capability_file(tmp_path, installed):
    ops = FakeOps()
    result = sb.install_hooks(ops, "claude", installed, staging_dir=tmp_path)
    document = json.loads(written(ops, result.settings_path))
    for groups in document["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                assert CONFIG in hook["command"]
                assert SHIM in hook["command"]


def test_no_fixture_carries_host_specific_configuration():
    """The user's real host config was the format reference; none of it, and no
    host path, may end up in a sandbox."""
    checked = [p for p in FIXTURES.iterdir() if p.name != "README.md"]
    assert len(checked) >= 6, "fixtures missing; scanning an empty set proves nothing"
    for path in checked:
        text = path.read_text()
        for forbidden in ("/Users/", "dot-agents", "nvim-notify", "amux-event.sh"):
            assert forbidden not in text, f"{path.name} leaks host configuration"
