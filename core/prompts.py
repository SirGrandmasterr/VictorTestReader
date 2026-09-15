"""Editing modes and prompt construction.

Built-in modes live in ``PROMPTS``. Users can add *custom modes* (a name and
an instruction, kept in ``AppSettings.custom_modes``) and *chains* (a name
and an ordered list of built-in or custom mode names, ``AppSettings.chains``)
that run one after another, each step editing the previous step's output.
"""

PROMPTS = {
    "Grammar": "Fix grammar issues without altering the meaning.",
    "Proofread": (
        "Proofread the text comprehensively, correcting errors and improving "
        "readability."
    ),
    "Natural": (
        "Refine awkward phrasing to make the text feel natural while preserving "
        "the original meaning."
    ),
    "Streamline": (
        "Remove unnecessary elements, clarify the message, and ensure coherence "
        "and ease of understanding."
    ),
    "Awkward": (
        "Fix only awkward or poorly written sentences without making other changes."
    ),
    "Rewrite": "Rewrite the text to improve clarity, flow, and overall readability.",
    "Concise": (
        "Make the text more concise by removing redundancy and unnecessary content."
    ),
    "Polish": (
        "Refine awkward words or phrases to give the text a polished and "
        "professional tone."
    ),
    "Improve": (
        "Enhance the text by proofreading and improving its clarity, flow, and "
        "coherence."
    ),
}

EDITING_MODES = list(PROMPTS) + ["Translate", "Custom"]
# Modes that need input at request time cannot be chain steps.
CHAIN_EXCLUDED = ("Translate", "Custom")
MAX_MODE_NAME = 40
MAX_INSTRUCTION = 2000
MAX_CHAIN_STEPS = 8


def custom_mode_map(custom_modes):
    """Return ``{name: instruction}`` from a list of ``{"name", "instruction"}`` dicts (or a dict)."""
    if not custom_modes:
        return {}
    if isinstance(custom_modes, dict):
        return dict(custom_modes)
    return {entry["name"]: entry["instruction"] for entry in custom_modes}


def build_instruction(mode, extra_value=None, custom_modes=None):
    """Return the LLM instruction for an editing mode.

    ``custom_modes`` (a ``{name: instruction}`` dict or the settings list) is
    consulted after the built-in modes; ``extra_value`` is the target language
    for Translate or the instruction for Custom.
    """
    if mode in PROMPTS:
        return PROMPTS[mode]
    custom = custom_mode_map(custom_modes)
    if mode in custom:
        return custom[mode]
    if mode == "Translate":
        return (
            "Translate the text into {0}. Preserve paragraphs and formatting."
        ).format(extra_value)
    if mode == "Custom":
        return extra_value or ""
    raise ValueError("Unknown editing mode: {0}".format(mode))


def build_chain(steps, custom_modes=None):
    """Return the instruction of every step of a chain, validating the step names.

    Raises ``ValueError`` for an empty chain, an unknown step name or a step
    that needs input at request time (``Translate``, ``Custom``).
    """
    steps = list(steps or [])
    if not steps:
        raise ValueError("A chain needs at least one step.")
    if len(steps) > MAX_CHAIN_STEPS:
        raise ValueError("A chain may have at most {0} steps.".format(MAX_CHAIN_STEPS))
    custom = custom_mode_map(custom_modes)
    instructions = []
    for step in steps:
        name = str(step).strip()
        if name in CHAIN_EXCLUDED:
            raise ValueError("{0} cannot be part of a chain.".format(name))
        if name in PROMPTS:
            instructions.append(PROMPTS[name])
        elif name in custom:
            instructions.append(custom[name])
        else:
            raise ValueError("Unknown chain step: {0}".format(name))
    return instructions


def _clean_name(value):
    return " ".join(str(value or "").split())[:MAX_MODE_NAME]


def validate_custom_modes(entries):
    """Return ``(modes, problems)``: the well-formed ``{"name", "instruction"}`` entries and why others were dropped.

    Names are trimmed and must be unique and distinct from the built-in
    modes; instructions must not be empty.
    """
    modes = []
    problems = []
    seen = set()
    for position, entry in enumerate(list(entries or []), 1):
        if not isinstance(entry, dict):
            problems.append("custom mode {0}: not an object".format(position))
            continue
        name = _clean_name(entry.get("name"))
        instruction = str(entry.get("instruction") or "").strip()[:MAX_INSTRUCTION]
        if not name:
            problems.append("custom mode {0}: empty name".format(position))
        elif name in EDITING_MODES or name.startswith("—"):
            problems.append("custom mode {0!r}: name is reserved".format(name))
        elif name in seen:
            problems.append("custom mode {0!r}: duplicate name".format(name))
        elif not instruction:
            problems.append("custom mode {0!r}: empty instruction".format(name))
        else:
            seen.add(name)
            modes.append({"name": name, "instruction": instruction})
    return modes, problems


def validate_chains(entries, custom_modes=None):
    """Return ``(chains, problems)`` for ``{"name", "steps"}`` entries (see ``build_chain``)."""
    chains = []
    problems = []
    custom = custom_mode_map(custom_modes)
    seen = set(custom)
    for position, entry in enumerate(list(entries or []), 1):
        if not isinstance(entry, dict):
            problems.append("chain {0}: not an object".format(position))
            continue
        name = _clean_name(entry.get("name"))
        steps = [str(step).strip() for step in (entry.get("steps") or []) if str(step).strip()]
        if not name:
            problems.append("chain {0}: empty name".format(position))
            continue
        if name in EDITING_MODES or name.startswith("—"):
            problems.append("chain {0!r}: name is reserved".format(name))
            continue
        if name in seen:
            problems.append("chain {0!r}: duplicate name".format(name))
            continue
        try:
            build_chain(steps, custom)
        except ValueError as exc:
            problems.append("chain {0!r}: {1}".format(name, exc))
            continue
        seen.add(name)
        chains.append({"name": name, "steps": steps})
    return chains, problems


def describe_chain(steps):
    """Human-readable step list ("Grammar → Polish")."""
    return " → ".join(str(step) for step in steps)
