"""The specification index: every series mapped, and every mapping checked against the file.

A hand-written mapping is correct on the day it is written and wrong by the next renumbering, with
nothing to notice. These tests open the actual specification files and assert the named heading is
present, so a section that moves breaks a test rather than a reader's trust.
"""
from pathlib import Path

import pytest

from api.spec_map import CROSS_CUTTING, REPO_ROOT, SPEC_MAP, spec_index
from strategy.registry import backtest_strategies


def test_every_registered_strategy_has_a_specification_entry():
    """A series in the code with no entry would be absent from the page, and a reader would have no
    way to discover that it exists."""
    registered = {s.id for s in backtest_strategies({})}
    assert registered == set(SPEC_MAP), (
        f"in the code only: {sorted(registered - set(SPEC_MAP))}; "
        f"in the index only: {sorted(set(SPEC_MAP) - registered)}"
    )
    assert len(registered) == 14, "twelve tradeable strategies plus two controls"


def test_every_specification_file_exists():
    for series, entry in SPEC_MAP.items():
        path = REPO_ROOT / entry["file"]
        assert path.exists(), f"{series} points at {entry['file']}, which does not exist"
    for doc in CROSS_CUTTING:
        assert (REPO_ROOT / doc["file"]).exists(), f"{doc['file']} does not exist"


def test_every_named_heading_is_actually_in_its_file():
    """The assertion that makes this index trustworthy. The heading is matched **literally**, so a
    renumbered or retitled section fails here instead of quietly misdirecting a reader."""
    for series, entry in SPEC_MAP.items():
        text = (REPO_ROOT / entry["file"]).read_text(encoding="utf-8")
        assert entry["heading"] in text, (
            f"{series}: {entry['heading']!r} is not in {entry['file']}. The section was probably "
            f"renumbered or retitled — update SPEC_MAP."
        )


def test_the_section_number_matches_the_heading():
    """`§4.2` and `### 4.2 ...` have to agree, or the page cites a section that is not the one it
    links to."""
    for series, entry in SPEC_MAP.items():
        number = entry["section"].lstrip("§")
        heading_number = entry["heading"].split()[1].rstrip(".")
        assert heading_number == number, (
            f"{series}: section says {number}, heading says {heading_number}"
        )


def test_the_series_name_appears_in_its_own_heading():
    for series, entry in SPEC_MAP.items():
        assert series in entry["heading"], f"{entry['heading']!r} does not name {series}"


def test_the_index_reports_the_implementation_read_from_the_registry(tmp_path):
    """The implementation half is introspected, not written down — so it cannot drift from the code
    the way a hand-maintained path would."""
    index = spec_index()
    by_id = {s["series"]: s for s in index["series"]}

    assert len(index["series"]) == 14
    assert index["unmapped"] == [] and index["orphaned"] == []

    c1c = by_id["C1-C"]
    assert c1c["implementation"] == "src/strategy/c1c_relative.py"
    assert isinstance(c1c["implementation_line"], int) and c1c["implementation_line"] > 0
    assert c1c["strategy_class"] == 1
    assert c1c["spec_file"] == "specs/03-class1-correction.md"

    # And the class really is defined there. `getsourcelines` reports the first line of the
    # *decorated* definition, which is `@dataclass`, so the `class` statement follows within a line
    # or two — hence a small window rather than an exact line.
    source = (REPO_ROOT / c1c["implementation"]).read_text(encoding="utf-8").splitlines()
    start = c1c["implementation_line"] - 1
    window = chr(10).join(source[start:start + 4])
    assert "class C1C" in window, window


def test_the_controls_are_marked_as_never_tradeable():
    """A control reaching a person would be indistinguishable from a real suggestion, so the page
    has to say which two they are."""
    by_id = {s["series"]: s for s in spec_index()["series"]}
    assert by_id["C4-X1"]["tradeable"] is False
    assert by_id["C4-X2"]["tradeable"] is False
    assert sum(1 for s in spec_index()["series"] if not s["tradeable"]) == 2
    for control in ("C4-X1", "C4-X2"):
        assert "control" in SPEC_MAP[control]["mechanism"]


def test_the_index_does_not_restate_any_trading_rule():
    """The specs are the only source of truth for rules. A threshold or formula copied here would be
    a second one, and the two would diverge.

    Checked crudely but usefully: no numeric threshold with a comparison operator, and no parameter
    name from the registry, appears in the prose.
    """
    prose = " ".join(
        [e["mechanism"] for e in SPEC_MAP.values()] + [e["title"] for e in SPEC_MAP.values()]
        + [d["detail"] for d in CROSS_CUTTING])
    for operator in ("≥", "<=", ">=", " > ", " < "):
        assert operator not in prose, f"a comparison ({operator}) suggests a restated threshold"
    for forbidden in ("z_rs", "rho_floor", "kappa", "act_trigger_multiple",
                      "stop_buffer_atr_multiple"):
        assert forbidden not in prose, f"{forbidden} is a parameter name; the specs own its value"


def test_the_route_serves_the_index(tmp_path):
    from fastapi.testclient import TestClient

    from api.app import create_app
    from api.services import build_services
    from store import open_store

    store = open_store(tmp_path / "s.db")
    try:
        signals, market, health = build_services(store)
        app = create_app(signals=signals, market=market, health=health)
        with TestClient(app) as client:
            response = client.get("/api/specs")
            assert response.status_code == 200
            body = response.json()["data"]
            assert len(body["series"]) == 14
            assert body["unmapped"] == []
            assert any(s["series"] == "C3-C" for s in body["series"])
            assert body["capability_map"]["file"] == "specs/00-capability-map.md"
    finally:
        store.close()


@pytest.mark.parametrize("series,expected_file,expected_section", [
    ("C1-C", "specs/03-class1-correction.md", "§5"),
    ("C2-C", "specs/04-class2-participation.md", "§5"),
    ("C3-C", "specs/05-class3-transition.md", "§5"),
    ("C4-C", "specs/06-class4-established.md", "§4.3"),
])
def test_the_series_named_in_the_request_map_where_expected(series, expected_file,
                                                            expected_section):
    """The four the owner asked about by name, pinned explicitly."""
    assert SPEC_MAP[series]["file"] == expected_file
    assert SPEC_MAP[series]["section"] == expected_section
    assert SPEC_MAP[series]["heading"] in (Path(REPO_ROOT) / expected_file).read_text(
        encoding="utf-8")
