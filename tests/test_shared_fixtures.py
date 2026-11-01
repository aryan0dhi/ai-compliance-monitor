"""docs/pod-interface/fixtures.json: the one copy of the B-20 fixtures and Model 5 scenarios."""

import json
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings
from app.modules.testing import fixtures as app_fixtures
from app.modules.testing.fixtures import FixtureError, baseline_systems, parse_fixtures
from tests.contract.fixtures import DEFAULT_PATH, PHASES, load_fixtures, parse_duration

RAW = json.loads(DEFAULT_PATH.read_text(encoding="utf-8"))
FX = load_fixtures()
README = Path(__file__).resolve().parents[1] / "docs" / "pod-interface" / "README.md"


def test_app_systems_match_the_file_field_for_field():
    systems = baseline_systems()
    assert [s.name for s in systems] == [s["name"] for s in RAW["systems"]]
    for ours, raw in zip(systems, RAW["systems"], strict=True):
        assert str(ours.id) == raw["id"]
        assert (ours.sampling_interval_s, ours.gap_missed_intervals) == (
            raw["sampling_interval_s"],
            raw["gap_missed_intervals"],
        )
        assert [
            (t.metric_name, t.metric_type.value, t.direction.value, t.limit_value)
            for t in ours.thresholds
        ] == [
            (t["metric_name"], t["metric_type"], t["direction"], Decimal(t["limit"]))
            for t in raw["thresholds"]
        ]


def test_readme_b20_table_matches_the_file():
    text = README.read_text(encoding="utf-8")
    sign = {"below": "<", "above": ">"}
    for system in RAW["systems"]:
        assert (
            f"| {system['name']} | `{system['id']}` | {system['sampling_interval_s']} s |" in text
        )
        for t in system["thresholds"]:
            row = next(r for r in text.splitlines() if f"| `{t['metric_name']}` |" in r)
            assert f"| {t['metric_type']} | `{sign[t['direction']]} {t['limit']}` |" in row


def test_every_scenario_names_a_fixture_system_and_metric():
    assert set(FX.scenarios) == {"Ex-1", "Ex-2", "Ex-3", "Ex-4", "Ex-5"}
    for scenario in FX.scenarios.values():
        assert scenario.system in FX.systems
        for step in scenario.steps:
            if "metric" in step.args:
                FX.threshold(scenario.system, metric=step.args["metric"])


def test_every_step_has_a_phase_and_a_duration():
    for scenario in FX.scenarios.values():
        ids = [s.id for s in scenario.steps]
        assert len(ids) == len(set(ids))
        for step in scenario.steps:
            assert step.phase in PHASES, step
            assert step.at.total_seconds() >= 0


def test_setup_refers_to_existing_steps():
    for scenario in FX.scenarios.values():
        if scenario.setup:
            other = FX.scenarios[scenario.setup["scenario"]]
            assert set(scenario.setup["steps"]) <= {s.id for s in other.steps}


@pytest.mark.parametrize("name", ["Ex-1", "Ex-2", "Ex-4"])
def test_scenarios_run_in_phase_3_have_a_phase_3_step(name):
    assert any(s.phase == 3 for s in FX.scenarios[name].steps)


def test_phase_gating_splits_run_and_deferred_steps():
    ex1 = FX.scenarios["Ex-1"]
    run, deferred = ex1.split([3], webhooks=True)
    assert {s.phase for s in run} == {3} and {s.phase for s in deferred} == {4, 5}
    assert len(run) + len(deferred) == len(ex1.steps)
    run, deferred = ex1.split([3], webhooks=False)
    assert "notified" in {s.id for s in deferred}
    run, deferred = ex1.split([3, 4, 5], webhooks=True)
    assert deferred == [] and len(run) == len(ex1.steps)
    assert ex1.split([], webhooks=True)[0] == []


@pytest.mark.parametrize(
    ("text", "seconds"), [("PT0S", 0), ("PT6M", 360), ("P10DT4H", 10 * 86400 + 4 * 3600)]
)
def test_parse_duration(text, seconds):
    assert parse_duration(text).total_seconds() == seconds


@pytest.mark.parametrize("text", ["", "P", "PT", "10D", "P1W", "P1DT", "-PT1S"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        parse_duration(text)


class TestLoading:
    def test_bad_data_raises(self):
        for broken in (
            {**RAW, "systems": []},
            {**RAW, "systems": [RAW["systems"][0], RAW["systems"][0]]},
            {**RAW, "valid_from": "2001-01-01T00:00:00Z"},
            {**RAW, "systems": [{**RAW["systems"][0], "id": "not-a-uuid"}]},
            {
                **RAW,
                "systems": [
                    {
                        **RAW["systems"][0],
                        "thresholds": [{**RAW["systems"][0]["thresholds"][0], "direction": "up"}],
                    }
                ],
            },
            {k: v for k, v in RAW.items() if k != "systems"},
        ):
            with pytest.raises(FixtureError):
                parse_fixtures(broken)

    @pytest.mark.parametrize("content", [None, "{not json"])
    def test_missing_or_unparsable_file_raises(self, tmp_path, monkeypatch, content):
        path = tmp_path / "fixtures.json"
        if content is not None:
            path.write_text(content, encoding="utf-8")
        monkeypatch.setattr(app_fixtures, "get_settings", lambda: Settings(fixtures_path=path))
        baseline_systems.cache_clear()
        try:
            with pytest.raises(FixtureError):
                baseline_systems()
        finally:
            baseline_systems.cache_clear()

    def test_contract_loader_honours_an_override(self, tmp_path, monkeypatch):
        path = tmp_path / "other.json"
        path.write_text(json.dumps({**RAW, "scenarios": {}}), encoding="utf-8")
        monkeypatch.setenv("CONTRACT_FIXTURES", str(path))
        assert load_fixtures().scenarios == {}
        assert re.fullmatch(r"[0-9a-f-]{36}", load_fixtures().systems["loan-scorer"]["id"])
