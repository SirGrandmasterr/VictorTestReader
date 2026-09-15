"""Visual theme: palettes, scalable named fonts and the ttk styles shared by every screen.

Colours come from ``PALETTE`` (and ``CHECK_COLORS`` / ``STATUS_COLORS`` for
the review), which ``select_palette`` fills from ``PALETTES`` - the normal
look or a high-contrast one with the same token names - so every screen keeps
reading the same dictionaries. Fonts are Tk *named fonts* created once by
``font()``; ``apply_scale`` resizes them all at once and every widget or ttk
style that uses them follows immediately (Ctrl+= / Ctrl+- in the app).

``apply_theme`` (re)configures the ttk styles for the current palette and
scale and then calls every callback registered with ``subscribe`` so screens
can restyle the plain Tk widgets (text areas, tree tags) they own.

State is never described by colour alone: ``STATUS_GLYPHS`` and
``DECISION_GLYPHS`` give each state a symbol that goes next to its word.
"""

import sys
import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

from core.settings import UI_SCALE_MAX, UI_SCALE_MIN

# "Paper, ink, clay, moss": warm linen and cream surfaces, charcoal text, one
# burnt-clay accent and earthy state colours. Every text-on-tint pair keeps
# at least 4.5:1. The header is light like the rest of the window.
PALETTES = {
    "normal": {
        "bg": "#f6f1e8",
        "surface": "#fffdf8",
        "surface_alt": "#efe8dc",
        "border": "#e3dacb",
        "border_strong": "#cfc3b0",
        "text": "#2a2521",
        "muted": "#6d655b",
        "faint": "#a0968a",
        "accent": "#a84a25",
        "accent_dark": "#8c3b1a",
        "accent_soft": "#f6e4d8",
        "header": "#fffdf8",
        "header_text": "#2a2521",
        "header_muted": "#6d655b",
        "header_active": "#fffdf8",
        "header_hover": "#efe8dc",
        "success": "#3d7a4f",
        "success_soft": "#e4efe3",
        "danger": "#b4463a",
        "danger_soft": "#f7e2dd",
        "warning": "#8f6212",
        "warning_soft": "#f7ebcf",
        "info": "#3f6a8c",
        "info_soft": "#e1eaf1",
        "selection": "#f3e7d3",
        "focus": "#a84a25",
    },
    # Black text on white, saturated dark state colours, thick black borders and
    # a yellow selection: readable for low vision and with Windows high contrast.
    "high_contrast": {
        "bg": "#ffffff",
        "surface": "#ffffff",
        "surface_alt": "#eeeeee",
        "border": "#000000",
        "border_strong": "#000000",
        "text": "#000000",
        "muted": "#222222",
        "faint": "#444444",
        "accent": "#7a2e0e",
        "accent_dark": "#5a1f06",
        "accent_soft": "#ffe0cf",
        "header": "#ffffff",
        "header_text": "#000000",
        "header_muted": "#222222",
        "header_active": "#ffe0cf",
        "header_hover": "#eeeeee",
        "success": "#005c00",
        "success_soft": "#ccffcc",
        "danger": "#a80000",
        "danger_soft": "#ffd6d6",
        "warning": "#703800",
        "warning_soft": "#ffe0a3",
        "info": "#003399",
        "info_soft": "#cce0ff",
        "selection": "#ffff99",
        "focus": "#000000",
    },
}
# Four inks that stay apart in hue and lightness for the common colour-vision deficiencies.
CHECK_PALETTES = {
    "normal": {
        "spelling": ("#7a4c8e", "#efe6f3"),
        "grammar": ("#3f6a8c", "#e1eaf1"),
        "expression": ("#2e7566", "#dff0e9"),
        "author": ("#9a4a1e", "#f8e6d8"),  # the author's own corrections (core.workflow.CHECK_AUTHOR)
    },
    "high_contrast": {
        "spelling": ("#3a0088", "#e6d6ff"),
        "grammar": ("#002a99", "#d1e0ff"),
        "expression": ("#004d44", "#bff2e8"),
        "author": ("#6b2400", "#ffdcbf"),
    },
}
PALETTE = {}  # filled by select_palette(); other modules import this dict and read it at use time
CHECK_COLORS = {}
STATUS_COLORS = {}
PALETTE_NAME = "normal"

# Symbols shown next to the state word so nothing depends on colour alone.
# Geometric shapes only: they come from one text font and take the label's
# colour, unlike the hourglass or warning sign that Windows draws as colour emoji.
STATUS_GLYPHS = {
    "queued": "◌",  # dotted circle
    "running": "◐",  # half disc
    "clean": "✓",  # check: nothing to do
    "ready": "○",  # ring: to review
    "reviewed": "●",  # filled disc: done
    "error": "■",  # square
    "flagged": "◆",  # diamond
}
DECISION_GLYPHS = {
    "applied": "✓",
    "superseded": "↷",  # arrow: another change took precedence
    "rejected": "✗",
    "pending": "○",  # empty circle
    "edited": "✎",  # pencil
}
# Text markers the chapter view can put in front of each change span.
SPAN_MARKERS = {
    "applied": "[+]",
    "pending": "[~]",
    "rejected": "[−]",
    "superseded": "[−]",
}

_FAMILY = None
_MONO_FAMILY = None
_SERIF_FAMILY = None
_FONTS = {}  # named font -> (tkfont.Font, base size)
_STANDARD_FONTS = {}  # Tk's own named fonts (TkDefaultFont, ...) -> base size, captured once
_scale = 1.0
_root = None  # the Tk root the named fonts belong to (set by apply_theme)
_subscribers = []


def select_palette(high_contrast=False):
    """Fill PALETTE / CHECK_COLORS / STATUS_COLORS in place; returns the palette name."""
    global PALETTE_NAME
    PALETTE_NAME = "high_contrast" if high_contrast else "normal"
    PALETTE.clear()
    PALETTE.update(PALETTES[PALETTE_NAME])
    CHECK_COLORS.clear()
    CHECK_COLORS.update(CHECK_PALETTES[PALETTE_NAME])
    STATUS_COLORS.clear()
    STATUS_COLORS.update({
        "queued": PALETTE["faint"],
        "running": PALETTE["info"],
        "error": PALETTE["danger"],
        "clean": PALETTE["success"],
        "ready": PALETTE["accent"],
        "reviewed": PALETTE["success"],
    })
    return PALETTE_NAME


select_palette(False)


def is_high_contrast():
    return PALETTE_NAME == "high_contrast"


# ------------------------------------------------------------------ fonts
def font_family():
    """Return the UI font family available on this platform."""
    global _FAMILY
    if _FAMILY is None:
        preferred = ["Segoe UI", "SF Pro Text", "Helvetica Neue", "Noto Sans", "DejaVu Sans", "Arial"]
        try:
            available = set(tkfont.families())
        except tk.TclError:
            available = set()
        _FAMILY = next((name for name in preferred if name in available), "TkDefaultFont")
    return _FAMILY


def mono_family():
    """Return a monospaced family for status details and the like."""
    global _MONO_FAMILY
    if _MONO_FAMILY is None:
        preferred = ["Cascadia Mono", "Consolas", "Menlo", "DejaVu Sans Mono", "Courier New"]
        try:
            available = set(tkfont.families())
        except tk.TclError:
            available = set()
        _MONO_FAMILY = next((name for name in preferred if name in available), "TkFixedFont")
    return _MONO_FAMILY


def serif_family():
    """Return a serif family for the author's own text (editor, diff, segments) so a manuscript reads like one."""
    global _SERIF_FAMILY
    if _SERIF_FAMILY is None:
        preferred = ["Georgia", "Constantia", "Cambria", "Palatino Linotype", "Charter", "DejaVu Serif", "Times New Roman"]
        try:
            available = set(tkfont.families())
        except tk.TclError:
            available = set()
        _SERIF_FAMILY = next((name for name in preferred if name in available), font_family())
    return _SERIF_FAMILY


def clamp_scale(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = 1.0
    return round(min(UI_SCALE_MAX, max(UI_SCALE_MIN, value)), 1)


def current_scale():
    return _scale


def scaled(size):
    """A point size (or pixel count) under the current scale, never below 6."""
    return max(6, int(round(size * _scale)))


def font(size=10, weight="normal", slant="roman", mono=False, serif=False):
    """Return the name of a shared named font of that base size and style.

    Named fonts are created once per (size, weight, slant, family) and resized
    together by ``apply_scale``; pass the returned name wherever Tk expects a
    font. Needs a Tk root (the app creates it before any widget).
    """
    family = mono_family() if mono else serif_family() if serif else font_family()
    name = "Teai{0}{1}{2}{3}".format("Mono" if mono else "Serif" if serif else "", size, weight.title(),
                                     "" if slant == "roman" else slant.title())
    entry = _FONTS.get(name)
    if entry is None:
        options = {"family": family, "size": scaled(size), "weight": weight, "slant": slant}
        try:
            created = tkfont.Font(root=_root, name=name, **options)
        except tk.TclError:  # the interpreter already has it (theme re-applied on a new root)
            created = tkfont.Font(root=_root, name=name, exists=True)
            created.configure(**options)
        _FONTS[name] = (created, size)
    return name


FONT_ROLES = {
    "body": (10, "normal", "roman"),
    "body_bold": (10, "bold", "roman"),
    "small": (9, "normal", "roman"),
    "small_bold": (9, "bold", "roman"),
    "small_italic": (9, "normal", "italic"),
    "heading": (11, "bold", "roman"),
    "title": (16, "normal", "roman"),  # serif, see named_font
    "text": (12, "normal", "roman"),  # serif: the author's words
}
SERIF_ROLES = ("title", "text")


def named_font(role):
    """The shared font for a role in ``FONT_ROLES`` (``"mono"`` is the monospaced body font)."""
    if role == "mono":
        return font(10, mono=True)
    size, weight, slant = FONT_ROLES[role]
    return font(size, weight, slant, serif=role in SERIF_ROLES)


def apply_scale(factor):
    """Resize every named font to ``factor`` times its base size; returns the clamped factor.

    Tk's standard fonts (``TkDefaultFont``, ``TkTextFont``, ...) are scaled as
    well, so entries, comboboxes, list boxes, menus and message boxes that
    never had an explicit font follow along.
    """
    global _scale
    _scale = clamp_scale(factor)
    for created, base in _FONTS.values():
        try:
            created.configure(size=scaled(base))
        except tk.TclError:
            pass
    for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont", "TkFixedFont", "TkCaptionFont",
                 "TkTooltipFont", "TkIconFont", "TkSmallCaptionFont"):
        try:
            standard = tkfont.nametofont(name, root=_root)
            if name not in _STANDARD_FONTS:
                _STANDARD_FONTS[name] = int(standard.cget("size"))
            base = _STANDARD_FONTS[name]
            # negative sizes are pixels; keep the sign
            standard.configure(size=scaled(abs(base)) * (-1 if base < 0 else 1))
        except (tk.TclError, ValueError, TypeError):
            continue
    return _scale


# ------------------------------------------------------------ subscribers
def subscribe(callback):
    """Call ``callback()`` after every ``apply_theme`` (palette or scale change)."""
    if callback not in _subscribers:
        _subscribers.append(callback)
    return callback


def unsubscribe(callback):
    if callback in _subscribers:
        _subscribers.remove(callback)


def bind_restyle(widget, callback):
    """Subscribe ``callback`` for the lifetime of ``widget``."""
    subscribe(callback)
    widget.bind("<Destroy>", lambda event: unsubscribe(callback) if event.widget is widget else None, add="+")


def _notify():
    for callback in list(_subscribers):
        try:
            callback()
        except tk.TclError:
            unsubscribe(callback)


# ----------------------------------------------------------------- styles
def apply_theme(root, high_contrast=None, scale=None):
    """Configure ttk styles and default widget colours on ``root``.

    ``high_contrast`` selects the palette and ``scale`` the font scale; both
    keep their current value when omitted. Safe to call again at runtime: the
    ttk styles and named fonts update in place and the subscribers restyle
    their plain Tk widgets.
    """
    global _root
    _root = root
    if high_contrast is not None:
        select_palette(bool(high_contrast))
    if scale is not None:
        apply_scale(scale)
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass
    base = font(10)
    small = font(9)
    small_bold = font(9, "bold")
    body_bold = font(10, "bold")
    hc = is_high_contrast()
    border_width = 2 if hc else 1
    strong = PALETTE["border_strong"]
    root.configure(background=PALETTE["bg"])
    root.option_add("*TCombobox*Listbox.font", base)
    root.option_add("*TCombobox*Listbox.selectBackground", PALETTE["accent"])
    root.option_add("*Menu.font", base)
    root.option_add("*Listbox.font", base)

    style.configure(".", background=PALETTE["bg"], foreground=PALETTE["text"], font=base,
                    bordercolor=PALETTE["border"], focuscolor=PALETTE["focus"], focusthickness=2 if hc else 1)
    style.configure("TFrame", background=PALETTE["bg"])
    style.configure("Surface.TFrame", background=PALETTE["surface"])
    # A sheet of paper on the desk: a hairline border, no shadow.
    style.configure("Card.TFrame", background=PALETTE["surface"], relief="solid", borderwidth=border_width,
                    bordercolor=strong)
    # The selected (keyboard-focused) card: a thick accent border, not only a tinted background.
    style.configure("Selected.Card.TFrame", background=PALETTE["selection"], bordercolor=PALETTE["focus"],
                    borderwidth=2, relief="solid")
    style.configure("Selected.TFrame", background=PALETTE["selection"])
    style.configure("Header.TFrame", background=PALETTE["header"])
    style.configure("Toolbar.TFrame", background=PALETTE["surface"])
    # The track behind the segmented mode toggle in the header.
    style.configure("Segment.TFrame", background=PALETTE["surface_alt"], relief="solid", borderwidth=border_width,
                    bordercolor=PALETTE["border"])

    style.configure("TLabel", background=PALETTE["bg"], foreground=PALETTE["text"], font=base)
    style.configure("Surface.TLabel", background=PALETTE["surface"])
    style.configure("Selected.TLabel", background=PALETTE["selection"])
    style.configure("Title.TLabel", background=PALETTE["bg"], font=named_font("title"))
    style.configure("Heading.TLabel", background=PALETTE["bg"], font=font(11, "bold"))
    style.configure("CardTitle.TLabel", background=PALETTE["surface"], font=font(11, "bold"))
    style.configure("Muted.TLabel", background=PALETTE["bg"], foreground=PALETTE["muted"])
    style.configure("Small.TLabel", background=PALETTE["bg"], font=small)
    style.configure("SmallMuted.TLabel", background=PALETTE["bg"], foreground=PALETTE["muted"], font=small)
    style.configure("SmallBold.TLabel", background=PALETTE["bg"], foreground=PALETTE["muted"], font=small_bold)
    style.configure("SurfaceMuted.TLabel", background=PALETTE["surface"], foreground=PALETTE["muted"], font=small)
    style.configure("SelectedMuted.TLabel", background=PALETTE["selection"], foreground=PALETTE["muted"], font=small)
    style.configure("Explanation.TLabel", background=PALETTE["surface"], foreground=PALETTE["muted"],
                    font=font(9, slant="italic"))
    style.configure("SelectedExplanation.TLabel", background=PALETTE["selection"], foreground=PALETTE["muted"],
                    font=font(9, slant="italic"))
    style.configure("Toolbar.TLabel", background=PALETTE["surface"], foreground=PALETTE["muted"])
    # The wordmark: a serif name on the light header, no bold.
    style.configure("Header.TLabel", background=PALETTE["header"], foreground=PALETTE["header_text"],
                    font=font(17, serif=True))
    style.configure("HeaderMuted.TLabel", background=PALETTE["header"], foreground=PALETTE["header_muted"],
                    font=small)
    style.configure("Status.TLabel", background=PALETTE["bg"], foreground=PALETTE["muted"])
    for state, color in STATUS_COLORS.items():
        style.configure("{0}.Status.TLabel".format(state.title()), background=PALETTE["bg"],
                        foreground=color, font=small_bold)
    for check, (fg, bg) in CHECK_COLORS.items():
        style.configure("{0}.Badge.TLabel".format(check.title()), background=bg, foreground=fg,
                        font=small_bold, padding=(7, 2), relief="solid" if hc else "flat", borderwidth=1,
                        bordercolor=fg)
    for name, fg, bg in (
        ("Applied", PALETTE["success"], PALETTE["success_soft"]),
        ("Superseded", PALETTE["warning"], PALETTE["warning_soft"]),
        ("Rejected", PALETTE["danger"], PALETTE["danger_soft"]),
        ("Pending", PALETTE["info"], PALETTE["info_soft"]),
        ("Mixed", PALETTE["warning"], PALETTE["warning_soft"]),  # quick review: partially accepted
    ):
        style.configure("{0}.State.TLabel".format(name), background=bg, foreground=fg,
                        font=small_bold, padding=(7, 2), relief="solid" if hc else "flat", borderwidth=1,
                        bordercolor=fg)
        style.configure("{0}.ReviewDecision.TLabel".format(name), background=PALETTE["bg"], foreground=fg,
                        font=body_bold)
    # Ochre badge for changes the hallucination guard flagged.
    style.configure("Flag.Badge.TLabel", background=PALETTE["warning_soft"], foreground=PALETTE["warning"],
                    font=small_bold, padding=(7, 2), relief="solid" if hc else "flat", borderwidth=1,
                    bordercolor=PALETTE["warning"])
    # Quiet tag naming the kind of edit (punctuation, spelling, ...).
    style.configure("Kind.Badge.TLabel", background=PALETTE["surface_alt"], foreground=PALETTE["muted"],
                    font=small, padding=(7, 2))
    # The numbered hints over an empty editor.
    style.configure("Step.TLabel", background=PALETTE["surface"], font=named_font("text"))
    style.configure("StepNumber.TLabel", background=PALETTE["surface"], foreground=PALETTE["accent"],
                    font=named_font("text"))
    # A keyboard shortcut printed next to a button ("Alt+A").
    style.configure("Kbd.TLabel", background=PALETTE["bg"], foreground=PALETTE["faint"], font=small)
    style.configure("Surface.Kbd.TLabel", background=PALETTE["surface"])
    style.configure("Selected.Kbd.TLabel", background=PALETTE["selection"])

    style.configure("TButton", background=PALETTE["surface"], foreground=PALETTE["text"], padding=(11, 6),
                    borderwidth=border_width, bordercolor=strong, relief="solid" if hc else "flat",
                    font=base)
    style.map("TButton",
              background=[("disabled", PALETTE["surface_alt"]), ("pressed", PALETTE["border"]),
                          ("active", PALETTE["surface_alt"])],
              foreground=[("disabled", PALETTE["faint"])],
              bordercolor=[("focus", PALETTE["focus"]), ("active", PALETTE["accent"])])
    for name in ("Accent.TButton", "Primary.TButton"):
        style.configure(name, background=PALETTE["accent"], foreground="#ffffff",
                        bordercolor=PALETTE["accent"], font=body_bold)
        style.map(name,
                  background=[("disabled", PALETTE["accent_soft"]), ("pressed", PALETTE["accent_dark"]),
                              ("active", PALETTE["accent_dark"])],
                  foreground=[("disabled", PALETTE["muted"])],
                  bordercolor=[("focus", PALETTE["focus"])])
    style.configure("Success.TButton", foreground=PALETTE["success"], bordercolor=PALETTE["success"])
    style.map("Success.TButton", background=[("active", PALETTE["success_soft"])])
    # The one filled green button of a screen: the accept action.
    style.configure("Fill.Success.TButton", background=PALETTE["success"], foreground="#ffffff",
                    bordercolor=PALETTE["success"], font=body_bold)
    style.map("Fill.Success.TButton",
              background=[("disabled", PALETTE["success_soft"]), ("pressed", PALETTE["success"]),
                          ("active", PALETTE["success"])],
              foreground=[("disabled", PALETTE["muted"])], bordercolor=[("focus", PALETTE["focus"])])
    style.configure("Danger.TButton", foreground=PALETTE["danger"], bordercolor=PALETTE["danger"])
    style.map("Danger.TButton", background=[("active", PALETTE["danger_soft"])])
    style.configure("Ghost.TButton", background=PALETTE["bg"], bordercolor=PALETTE["border"] if hc else PALETTE["bg"],
                    foreground=PALETTE["muted"])
    style.map("Ghost.TButton", background=[("active", PALETTE["surface"])], foreground=[("active", PALETTE["text"])],
              bordercolor=[("focus", PALETTE["focus"])])
    style.configure("Surface.Ghost.TButton", background=PALETTE["surface"],
                    bordercolor=PALETTE["border"] if hc else PALETTE["surface"])
    style.map("Surface.Ghost.TButton", background=[("active", PALETTE["surface_alt"])],
              foreground=[("active", PALETTE["text"])], bordercolor=[("focus", PALETTE["focus"])])
    # Segments of the mode toggle: the active one is a raised sheet with the accent word.
    style.configure("Nav.TButton", background=PALETTE["surface_alt"], foreground=PALETTE["header_muted"],
                    bordercolor=PALETTE["surface_alt"], padding=(14, 4))
    style.map("Nav.TButton", background=[("active", PALETTE["header_hover"])],
              foreground=[("active", PALETTE["header_text"])],
              bordercolor=[("focus", PALETTE["focus"])])
    style.configure("NavActive.TButton", background=PALETTE["header_active"], foreground=PALETTE["accent"],
                    bordercolor=strong, padding=(14, 4), font=body_bold)
    style.map("NavActive.TButton", background=[("active", PALETTE["header_active"])],
              bordercolor=[("focus", PALETTE["focus"])])
    style.configure("Small.TButton", padding=(7, 3), font=small)
    style.configure("Small.Success.TButton", padding=(7, 3), font=small, foreground=PALETTE["success"],
                    bordercolor=PALETTE["success"])
    style.configure("Small.Fill.Success.TButton", padding=(7, 3), font=small_bold)
    style.configure("Small.Danger.TButton", padding=(7, 3), font=small, foreground=PALETTE["danger"],
                    bordercolor=PALETTE["danger"])
    style.configure("Small.Ghost.TButton", padding=(7, 3), font=small)
    style.configure("Small.Surface.Ghost.TButton", padding=(7, 3), font=small)
    style.configure("Link.TButton", padding=(4, 1), font=small, foreground=PALETTE["accent"],
                    background=PALETTE["surface"], bordercolor=PALETTE["accent"] if hc else PALETTE["surface"])
    style.map("Link.TButton", background=[("active", PALETTE["surface_alt"])],
              bordercolor=[("focus", PALETTE["focus"])])
    style.configure("Selected.Link.TButton", background=PALETTE["selection"],
                    bordercolor=PALETTE["accent"] if hc else PALETTE["selection"])
    style.map("Selected.Link.TButton", background=[("active", PALETTE["surface"])],
              bordercolor=[("focus", PALETTE["focus"])])
    # A pill in the header: the connection state and model, opens the connection menu.
    style.configure("Pill.TMenubutton", background=PALETTE["surface"], foreground=PALETTE["text"],
                    bordercolor=strong, borderwidth=border_width, relief="solid" if hc else "flat",
                    padding=(10, 4), font=small, arrowsize=0)
    style.map("Pill.TMenubutton", background=[("active", PALETTE["surface_alt"])],
              bordercolor=[("focus", PALETTE["focus"])])
    for name, color in (("Ok", PALETTE["success"]), ("Warn", PALETTE["warning"]), ("Error", PALETTE["danger"])):
        style.configure("{0}.Pill.TMenubutton".format(name), foreground=color, font=small_bold)
    style.configure("TSeparator", background=PALETTE["border"])

    _borrow_native_indicators(style)
    style.configure("TCheckbutton", background=PALETTE["bg"], font=base)
    style.configure("Surface.TCheckbutton", background=PALETTE["surface"])
    style.configure("Toolbar.TCheckbutton", background=PALETTE["surface"])
    style.configure("TRadiobutton", background=PALETTE["bg"], font=base)
    style.configure("Surface.TRadiobutton", background=PALETTE["surface"])
    style.configure("TMenubutton", font=base)
    style.configure("Small.TMenubutton", padding=(6, 2), font=small)
    style.configure("TLabelframe", background=PALETTE["bg"], bordercolor=PALETTE["border"], relief="solid")
    style.configure("TLabelframe.Label", background=PALETTE["bg"], foreground=PALETTE["muted"], font=small_bold)
    style.configure("TEntry", fieldbackground=PALETTE["surface"], bordercolor=strong, padding=5,
                    font=base)
    style.map("TEntry", bordercolor=[("focus", PALETTE["focus"])])
    style.configure("TSpinbox", fieldbackground=PALETTE["surface"], bordercolor=strong, arrowsize=12,
                    padding=3)
    style.configure("TCombobox", fieldbackground=PALETTE["surface"], bordercolor=strong, padding=4,
                    arrowsize=14)
    style.map("TCombobox", fieldbackground=[("readonly", PALETTE["surface"])],
              selectbackground=[("readonly", PALETTE["surface"])],
              selectforeground=[("readonly", PALETTE["text"])],
              bordercolor=[("focus", PALETTE["focus"])])
    # Progress is moss green: how much of the manuscript is done, not an alert.
    style.configure("Horizontal.TProgressbar", troughcolor=PALETTE["surface_alt"], background=PALETTE["success"],
                    bordercolor=PALETTE["border"], lightcolor=PALETTE["success"], darkcolor=PALETTE["success"])
    style.configure("Treeview", background=PALETTE["surface"], fieldbackground=PALETTE["surface"],
                    foreground=PALETTE["text"], rowheight=scaled(27), bordercolor=PALETTE["border"], font=base)
    style.map("Treeview", background=[("selected", PALETTE["selection"])],
              foreground=[("selected", PALETTE["text"])])
    style.configure("Treeview.Heading", background=PALETTE["surface_alt"], foreground=PALETTE["muted"],
                    font=small_bold, relief="flat", padding=(6, 4))
    style.configure("TPanedwindow", background=PALETTE["bg"])
    style.configure("Sash", sashthickness=6, gripcount=0, background=PALETTE["bg"])
    style.configure("TScrollbar", background=PALETTE["surface_alt"], troughcolor=PALETTE["bg"],
                    bordercolor=PALETTE["bg"], arrowsize=12)
    style.configure("TNotebook", background=PALETTE["bg"], bordercolor=PALETTE["border"])
    style.configure("TNotebook.Tab", padding=(12, 5), background=PALETTE["surface_alt"], font=base)
    style.map("TNotebook.Tab", background=[("selected", PALETTE["surface"])],
              foreground=[("selected", PALETTE["accent"])],
              bordercolor=[("focus", PALETTE["focus"])])
    _notify()
    return style


def _borrow_native_indicators(style):
    """Use the platform's check/radio indicators instead of clam's boxes."""
    if sys.platform != "win32":
        return
    try:
        style.element_create("Native.Checkbutton.indicator", "from", "vista", "Checkbutton.indicator")
        style.element_create("Native.Radiobutton.indicator", "from", "vista", "Radiobutton.indicator")
    except tk.TclError:
        return  # already created (theme applied again) or no vista theme
    style.layout("TCheckbutton", [
        ("Checkbutton.padding", {"sticky": "nswe", "children": [
            ("Native.Checkbutton.indicator", {"side": "left", "sticky": ""}),
            ("Checkbutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Checkbutton.label", {"sticky": "nswe"})]}),
        ]}),
    ])
    style.layout("TRadiobutton", [
        ("Radiobutton.padding", {"sticky": "nswe", "children": [
            ("Native.Radiobutton.indicator", {"side": "left", "sticky": ""}),
            ("Radiobutton.focus", {"side": "left", "sticky": "w", "children": [
                ("Radiobutton.label", {"sticky": "nswe"})]}),
        ]}),
    ])


class Tooltip:
    """Show ``text`` in a small window while the pointer rests on ``widget``."""

    def __init__(self, widget, text, delay=450):
        self.widget = widget
        self.text = text
        self.delay = delay
        self._after = None
        self._window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")  # keyboard users get the hint, too
        widget.bind("<FocusOut>", self._hide, add="+")

    def _schedule(self, event=None):
        self._cancel()
        self._after = self.widget.after(self.delay, self._show)

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except tk.TclError:
                pass
            self._after = None

    def _show(self):
        self._after = None
        if self._window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_rootx() + 12
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            self._window = tk.Toplevel(self.widget)
            self._window.wm_overrideredirect(True)
            self._window.wm_geometry("+{0}+{1}".format(x, y))
            tk.Label(
                self._window, text=self.text, justify=tk.LEFT, wraplength=scaled(320),
                background=PALETTE["text"], foreground=PALETTE["surface"],
                font=font(9), padx=8, pady=5,
            ).pack()
        except tk.TclError:
            self._window = None

    def _hide(self, event=None):
        self._cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None


class RowTooltip(Tooltip):
    """A tooltip per Treeview row: ``texts`` maps row iids to what to show while the pointer rests on them."""

    def __init__(self, tree, texts, delay=500):
        super().__init__(tree, "", delay=delay)
        self.texts = texts
        self._row = None
        tree.bind("<Motion>", self._moved, add="+")

    def _moved(self, event):
        row = self.widget.identify_row(event.y)
        if row == self._row:
            return
        self._row = row
        self._hide()
        self.text = self.texts.get(row, "")
        if self.text:
            self._schedule()

    def _show(self):
        self._after = None
        if self._window is not None or not self.text:
            return
        try:
            x = self.widget.winfo_pointerx() + 14
            y = self.widget.winfo_pointery() + 18
            self._window = tk.Toplevel(self.widget)
            self._window.wm_overrideredirect(True)
            self._window.wm_geometry("+{0}+{1}".format(x, y))
            tk.Label(
                self._window, text=self.text, justify=tk.LEFT, wraplength=scaled(320),
                background=PALETTE["text"], foreground=PALETTE["surface"],
                font=font(9), padx=8, pady=5,
            ).pack()
        except tk.TclError:
            self._window = None


def style_text(widget, size=11, background=None, readonly=False, mono=False, serif=False):
    """Apply theme colours and the scalable font to a tk.Text widget (call again after a palette change).

    ``serif`` marks a widget that holds the author's own words: it gets the
    serif family and the margins of a page rather than of a form field.
    """
    widget.configure(
        font=font(size, mono=mono, serif=serif),
        background=background or PALETTE["surface"],
        foreground=PALETTE["text"],
        insertbackground=PALETTE["accent"],
        selectbackground=PALETTE["accent_soft"],
        selectforeground=PALETTE["text"],
        inactiveselectbackground=PALETTE["accent_soft"],  # the selection stays visible while a button has focus
        relief=tk.FLAT,
        highlightthickness=2 if is_high_contrast() else 1,
        highlightbackground=PALETTE["border_strong"],
        highlightcolor=PALETTE["focus"],
        padx=scaled(22) if serif else 10,
        pady=scaled(14) if serif else 8,
        spacing1=2 if serif else 1,
        spacing3=6 if serif else 3,
        wrap=tk.WORD,
    )
    if readonly:
        widget.configure(state=tk.DISABLED, cursor="arrow")
        if sys.platform == "win32":
            widget.configure(takefocus=0)
    return widget


class ScrollableFrame(ttk.Frame):
    """A vertically scrollable container (Canvas + inner Frame)."""

    def __init__(self, parent, background=None, **kwargs):
        super().__init__(parent, **kwargs)
        self._surface = background == PALETTE["surface"] if background else False
        self.canvas = tk.Canvas(self, background=background or PALETTE["bg"], highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas, style="Surface.TFrame" if self._surface else "TFrame")
        self._window = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda event: self._bind_wheel())
        self.canvas.bind("<Leave>", lambda event: self._unbind_wheel())
        bind_restyle(self, self.restyle)

    def restyle(self):
        self.canvas.configure(background=PALETTE["surface"] if self._surface else PALETTE["bg"])

    def _on_inner_configure(self, event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._window, width=event.width)

    def _bind_wheel(self):
        self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        self.canvas.bind_all("<Button-4>", self._on_wheel)
        self.canvas.bind_all("<Button-5>", self._on_wheel)

    def _unbind_wheel(self):
        self.canvas.unbind_all("<MouseWheel>")
        self.canvas.unbind_all("<Button-4>")
        self.canvas.unbind_all("<Button-5>")

    def _on_wheel(self, event):
        if getattr(event, "num", None) == 4:
            self.canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            self.canvas.yview_scroll(1, "units")
        else:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")

    def scroll_to_widget(self, widget):
        """Scroll so that ``widget`` (a child of ``inner``) is visible."""
        try:
            if not widget.winfo_exists():
                return
            self.update_idletasks()
        except tk.TclError:
            return
        total = max(1, self.inner.winfo_height())
        top = widget.winfo_y()
        bottom = top + widget.winfo_height()
        view_top = self.canvas.canvasy(0)
        view_bottom = view_top + self.canvas.winfo_height()
        if top < view_top:
            self.canvas.yview_moveto(top / total)
        elif bottom > view_bottom:
            self.canvas.yview_moveto(max(0, bottom - self.canvas.winfo_height()) / total)

    def clear(self):
        for child in self.inner.winfo_children():
            child.destroy()
        self.canvas.yview_moveto(0)
