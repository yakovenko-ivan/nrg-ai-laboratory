from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from campaign_tools.campaign_generator_1d_flame import (
    expand_campaign,
    read_campaign,
    render_namelist,
    validate_case,
)


EXAMPLE = (
    Path(__file__).resolve().parent.parent
    / "campaign_tools"
    / "examples"
    / "campaign_1d_wall_flame_keromnes.toml"
)


def test_keromnes_example_expands_to_288_cases() -> None:
    data = read_campaign(EXAMPLE)
    lab = SimpleNamespace(runs_root=Path("runs"))
    cases = expand_campaign(data, lab)

    assert len(cases) == 288
    assert len({case["case_fingerprint"] for case in cases}) == 288
    assert {case["groups"]["geometry_config"]["coordinate_system"] for case in cases} == {
        "cartesian",
        "cylindrical",
        "spherical",
    }
    assert {case["groups"]["geometry_config"]["cell_size_m"] for case in cases} == {
        2.0e-4,
        4.0e-4,
    }
    assert {case["groups"]["mixture_config"]["hydrogen_mole_percent"] for case in cases} == set(
        float(value) for value in range(4, 16)
    )


def test_rendered_namelist_uses_millisecond_output_contract() -> None:
    data = read_campaign(EXAMPLE)
    lab = SimpleNamespace(runs_root=Path("runs"))
    case = expand_campaign(data, lab)[0]
    text = render_namelist(case["groups"])

    assert "postprocess_interval_ms = 1" in text
    assert "field_save_interval_ms = 1" in text
    assert "checkpoint_interval_ms = 5" in text
    assert "final_time_s = 30" in text


def test_non_aligned_ignition_width_is_rejected() -> None:
    data = read_campaign(EXAMPLE)
    lab = SimpleNamespace(runs_root=Path("runs"))
    groups = deepcopy(expand_campaign(data, lab)[0]["groups"])
    groups["ignition_config"]["ignition_width_m"] = 4.85e-3

    with pytest.raises(ValueError, match="ignition_width_m must be exactly cell-aligned"):
        validate_case(groups)
