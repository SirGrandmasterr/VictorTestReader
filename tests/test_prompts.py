"""Tests for editing modes, custom presets and chains (core/prompts.py)."""

import pytest

from core.prompts import (
    CHAIN_EXCLUDED,
    EDITING_MODES,
    MAX_CHAIN_STEPS,
    PROMPTS,
    build_chain,
    build_instruction,
    custom_mode_map,
    describe_chain,
    validate_chains,
    validate_custom_modes,
)

CUSTOM = [{"name": "House style", "instruction": "Apply the house style."}]


def test_built_in_modes_are_unchanged_and_custom_modes_are_looked_up():
    assert EDITING_MODES == list(PROMPTS) + ["Translate", "Custom"]
    assert build_instruction("Grammar") == PROMPTS["Grammar"]
    assert build_instruction("Translate", "German").startswith("Translate the text into German.")
    assert build_instruction("Custom", "Do it.") == "Do it."
    assert build_instruction("House style", custom_modes=CUSTOM) == "Apply the house style."
    assert build_instruction("House style", custom_modes=custom_mode_map(CUSTOM)) == "Apply the house style."
    with pytest.raises(ValueError):
        build_instruction("House style")
    with pytest.raises(ValueError):
        build_instruction("Nope", custom_modes=CUSTOM)


def test_build_chain_returns_one_instruction_per_step_and_validates():
    assert build_chain(["Grammar", "House style", "Polish"], CUSTOM) == [
        PROMPTS["Grammar"], "Apply the house style.", PROMPTS["Polish"]
    ]
    for name in CHAIN_EXCLUDED:
        with pytest.raises(ValueError):
            build_chain(["Grammar", name], CUSTOM)
    with pytest.raises(ValueError):
        build_chain(["Grammar", "Unknown"], CUSTOM)
    with pytest.raises(ValueError):
        build_chain([], CUSTOM)
    with pytest.raises(ValueError):
        build_chain(["Grammar"] * (MAX_CHAIN_STEPS + 1))
    assert describe_chain(["Grammar", "Polish"]) == "Grammar → Polish"


def test_custom_modes_are_validated_and_cleaned():
    modes, problems = validate_custom_modes([
        {"name": "  House   style ", "instruction": "  Apply it. "},
        {"name": "Grammar", "instruction": "x"},  # reserved
        {"name": "House style", "instruction": "dup"},
        {"name": "", "instruction": "x"},
        {"name": "Empty", "instruction": "   "},
        "not a dict",
        {"name": "— Chains —", "instruction": "x"},
    ])
    assert modes == [{"name": "House style", "instruction": "Apply it."}]
    assert len(problems) == 6
    assert any("reserved" in p for p in problems) and any("duplicate" in p for p in problems)
    assert validate_custom_modes(None) == ([], [])


def test_chains_are_validated_against_built_in_and_custom_names():
    chains, problems = validate_chains([
        {"name": "Tidy", "steps": ["Grammar", "House style"]},
        {"name": "Bad step", "steps": ["Grammar", "Translate"]},
        {"name": "House style", "steps": ["Grammar"]},  # collides with a custom mode
        {"name": "Tidy", "steps": ["Polish"]},  # duplicate
        {"name": "Empty", "steps": []},
        {"name": "Polish", "steps": ["Grammar"]},  # reserved
        {"name": "Unknown", "steps": ["Grammar", "Missing"]},
    ], CUSTOM)
    assert chains == [{"name": "Tidy", "steps": ["Grammar", "House style"]}]
    assert len(problems) == 6
    assert validate_chains([{"name": "Solo", "steps": ["Polish"]}]) == ([{"name": "Solo", "steps": ["Polish"]}], [])
