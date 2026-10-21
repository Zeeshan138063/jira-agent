import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

from jira_agent import cli, usage
from jira_agent.config import ProfileStore

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_AGENT_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("JIRA_AGENT_NO_USAGE", raising=False)
    return tmp_path / "home"


def entry(command, tokens, ts=None):
    return {"ts": ts or int(time.time()), "command": command, "tokens": tokens, "seconds": 0.5}


@pytest.mark.parametrize(
    "argv,name",
    [
        (["kb", "context", "--summary", "secret words"], "kb context"),
        (["jira", "get", "PROJ-1"], "jira get"),
        (["groom", "check", "--key", "PROJ-1"], "groom check"),
        (["usage"], "usage"),
        (["--help"], "(none)"),
        ([], "(none)"),
        (["session", "check"], "session check"),
    ],
)
def test_command_name_keeps_the_command_words_only(argv, name):
    assert usage.command_name(argv) == name


def test_estimate_rounds_up():
    assert usage.estimate_tokens(0) == 0 and usage.estimate_tokens(1) == 1
    assert usage.estimate_tokens(400) == 100 and usage.estimate_tokens(401) == 101


def test_counting_stream_counts_and_forwards(capsys):
    counted = usage.CountingStream(sys.stdout)
    counted.write("hello ")
    counted.write("world")
    counted.flush()
    assert counted.chars == 11 and capsys.readouterr().out == "hello world"
    assert hasattr(counted, "isatty")


def test_record_writes_a_private_line_and_load_reads_it_back(home):
    usage.record(entry("kb context", 900))
    path = usage.log_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert usage.load_entries()[0]["tokens"] == 900


def test_record_can_be_switched_off(home, monkeypatch):
    monkeypatch.setenv("JIRA_AGENT_NO_USAGE", "1")
    usage.record(entry("kb context", 1))
    assert not usage.log_path().exists()


def test_record_never_raises_when_the_disk_refuses(home, monkeypatch):
    def refuse(*args, **kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(os, "open", refuse)
    usage.record(entry("kb context", 1))  # must not raise


def test_the_log_is_trimmed_when_it_grows_too_large(home, monkeypatch):
    monkeypatch.setattr(usage, "TRIM_BYTES", 2000)
    monkeypatch.setattr(usage, "KEEP_LINES", 5)
    for i in range(60):
        usage.record(entry("kb search", i))
    kept = usage.load_entries()
    assert len(kept) <= 60 and kept[-1]["tokens"] == 59 and len(kept) < 60


def test_corrupt_lines_are_skipped(home):
    usage.record(entry("kb search", 10))
    with usage.log_path().open("a") as f:
        f.write("{not json\n")
    assert len(usage.load_entries()) == 1


def test_summary_totals_percentiles_and_heaviest_calls(home):
    now = int(time.time())
    rows = [entry("kb context", t, now) for t in (800, 900, 1000, 1100, 2000)]
    rows += [entry("kb search", 100, now), entry("groom check", 400, now - 40 * 86400)]
    s = usage.summarize(rows)
    assert (s["calls"], s["tokens"]) == (7, 800 + 900 + 1000 + 1100 + 2000 + 100 + 400)
    top = s["by_command"][0]
    assert top["command"] == "kb context" and top["calls"] == 5
    assert (top["avg"], top["max"], top["p90"]) == (1160, 2000, 2000)
    assert s["heaviest_calls"][0]["tokens"] == 2000
    assert len(s["by_day"]) == 2
    assert usage.summarize([]) == {
        "calls": 0,
        "tokens": 0,
        "avg_per_call": 0,
        "by_command": [],
        "by_day": {},
        "heaviest_calls": [],
    }


def test_days_filter(home):
    usage.record(entry("old", 1, int(time.time()) - 10 * 86400))
    usage.record(entry("new", 2))
    assert [e["command"] for e in usage.load_entries(days=3)] == ["new"]
    assert len(usage.load_entries()) == 2


def test_static_costs_cover_the_agent_and_every_skill():
    costs = usage.static_costs(ROOT)
    assert costs["agent"] > 0 and len(costs["skills"]) == 8
    assert costs["all_skills"] == sum(costs["skills"].values())
    assert "jira-groom" in costs["skills"]


def test_table_is_readable_and_mentions_the_estimate_rule():
    text = usage.render_table(usage.summarize([entry("kb context", 900)]), usage.static_costs(ROOT))
    assert "kb context" in text and "characters / 4" in text and "Instructions loaded" in text
    assert "No usage recorded" in usage.render_table(usage.summarize([]))


# --- the real entry point ---------------------------------------------------------------------


def run_main(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["jira-agent", *argv])
    return cli.main()


def test_main_meters_a_command_without_changing_its_output(home, monkeypatch, capsys):
    ProfileStore().save(
        __import__("jira_agent.config", fromlist=["Profile"]).Profile(
            name="p",
            site_url="https://acme.atlassian.net",
            projects=["PROJ"],
            kb_path=str(home / "kb"),
        )
    )
    assert run_main(monkeypatch, "profile", "list") == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["profiles"][0]["name"] == "p"
    logged = usage.load_entries()[0]
    assert logged["command"] == "profile list" and logged["exit"] == 0
    assert logged["chars"] == len(printed) and logged["tokens"] == usage.estimate_tokens(
        len(printed)
    )


def test_main_records_failures_too_and_counts_stderr(home, monkeypatch, capsys):
    assert run_main(monkeypatch, "profile", "show", "ghost") == 2
    capsys.readouterr()
    logged = usage.load_entries()[0]
    assert logged["exit"] == 2 and logged["tokens"] > 0


def test_arguments_and_values_never_reach_the_log(home, monkeypatch, capsys):
    run_main(monkeypatch, "profile", "show", "my-secret-profile-name")
    capsys.readouterr()
    assert "my-secret-profile-name" not in usage.log_path().read_text()


def test_the_usage_command_is_not_metered_and_shows_the_report(home, monkeypatch, capsys):
    usage.record(entry("kb context", 900))
    assert run_main(monkeypatch, "usage", "--format", "json", "--static") == 0
    report = json.loads(capsys.readouterr().out)
    assert report["calls"] == 1 and report["instructions"]["agent"] > 0
    assert len(usage.load_entries()) == 1


def test_usage_table_and_reset(home, monkeypatch, capsys):
    usage.record(entry("kb context", 900))
    assert run_main(monkeypatch, "usage") == 0
    assert "kb context" in capsys.readouterr().out
    assert run_main(monkeypatch, "usage", "--reset") == 0
    assert not usage.log_path().exists()
    assert run_main(monkeypatch, "usage") == 0
    assert "No usage recorded" in capsys.readouterr().out
