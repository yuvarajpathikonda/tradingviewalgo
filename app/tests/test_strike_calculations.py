import pytest

from main import (
    compute_strike_by_type,
)

SPOT = 22000
STEP = 50


@pytest.mark.parametrize(
    "strike_type, intent, expected",
    [
        # ---------- CE ----------
        ("ITM1", "CE", 21950),
        ("ITM2", "CE", 21900),
        ("ATM",  "CE", 22000),
        ("OTM1", "CE", 22050),
        ("OTM2", "CE", 22100),

        # ---------- PE ----------
        ("ITM1", "PE", 22050),
        ("ITM2", "PE", 22100),
        ("ATM",  "PE", 22000),
        ("OTM1", "PE", 21950),
        ("OTM2", "PE", 21900),
    ]
)
def test_ui_strike_type_mapping(strike_type, intent, expected):
    strike = compute_strike_by_type(
        spot=SPOT,
        step=STEP,
        intent=intent,
        strike_type=strike_type
    )
    assert strike == expected


def test_invalid_strike_type():
    with pytest.raises(ValueError):
        compute_strike_by_type(
            spot=SPOT,
            step=STEP,
            intent="CE",
            strike_type="INVALID"
        )


def test_case_insensitive_ui_values():
    strike = compute_strike_by_type(
        spot=SPOT,
        step=STEP,
        intent="ce",
        strike_type="itm1"
    )
    assert strike == 21950


def test_float_inputs_from_ui():
    strike = compute_strike_by_type(
        spot=22000.75,
        step=50.0,
        intent="PE",
        strike_type="OTM1"
    )
    assert strike == 21950
