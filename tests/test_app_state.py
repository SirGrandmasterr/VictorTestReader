"""Tests for generation result guards that do not require a Tk window."""

from core.diff_engine import build_edit_session
from ui.app import EditorApp


def make_session(revision_id):
    return build_edit_session("Original text.", "Proposed text.", instruction="Fix grammar.", model="model",
                              revision_id=revision_id)


def make_app_shell():
    app = EditorApp.__new__(EditorApp)
    app.active_request_id = 7
    app.revision_id = 4
    app.generating = True
    app.current_session = None
    app.status_messages = []
    app.finished = False
    app.set_status = app.status_messages.append

    def finish():
        app.finished = True
        app.generating = False

    app._finish_generation = finish
    return app


def test_stale_revision_result_is_discarded_before_session_creation():
    app = make_app_shell()
    event = ("generation_result", 7, 3, make_session(3))

    app._handle_generation_result(event)

    assert app.finished is True
    assert app.current_session is None
    assert "stale result from model was discarded" in app.status_messages[-1]


def test_result_from_superseded_request_is_ignored():
    app = make_app_shell()
    event = ("generation_result", 6, 4, make_session(4))

    app._handle_generation_result(event)

    assert app.finished is False
    assert app.current_session is None


def test_cancelled_active_request_restores_idle_state():
    app = make_app_shell()

    app._handle_generation_cancelled(7)

    assert app.finished is True
    assert app.generating is False
    assert app.status_messages[-1] == (
        "Generation cancelled. Your text was not changed."
    )


def test_selection_is_located_in_the_current_text_or_rejected():
    from core.diff_engine import build_edit_session

    full = "Start. Teh cat sat. End."
    session = build_edit_session("Teh cat sat.", "The cat sat.", selection=(7, 19), full_text=full)

    assert EditorApp._locate_selection(full, session) == (7, 19)
    # text inserted before the selection: the passage is found again when it is unique
    assert EditorApp._locate_selection("More. " + full, session) == (13, 25)
    assert EditorApp._locate_selection("Teh cat sat. Teh cat sat.", session) is None
    assert EditorApp._locate_selection("Gone.", session) is None


def test_window_title_shows_file_name_and_unsaved_marker():
    from ui.app import APP_TITLE, window_title

    assert window_title(None, False) == APP_TITLE
    assert window_title(None, True) == "Untitled \u2022 \u2014 TextEnhanceAI"
    assert window_title("C:/books/novel.docx", False) == "novel.docx \u2014 TextEnhanceAI"
    assert window_title("C:/books/novel.docx", True) == "novel.docx \u2022 \u2014 TextEnhanceAI"
