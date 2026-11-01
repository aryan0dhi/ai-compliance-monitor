"""docs/pod-interface/fixtures.json: the one copy of the B-20 fixtures."""

import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import Settings
from app.modules.testing import fixtures as app_fixtures
from app.modules.testing.fixtures import FixtureError, baseline_systems, parse_fixtures

ROOT = Path(__file__).resolve().parents[1]
RAW = json.loads((ROOT / "docs" / "pod-interface" / "fixtures.json").read_text(encoding="utf-8"))
README = ROOT / "docs" / "pod-interface" / "README.md"


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
