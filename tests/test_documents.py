"""Tests for loading .txt/.md/.docx/.odt manuscripts and writing edited text back."""

import zipfile
from xml.etree import ElementTree as ET

import pytest

from core.chunking import paragraphs, split_document
from core.documents import (
    FORMATTED_KINDS,
    KIND_DOCX,
    KIND_MD,
    KIND_ODT,
    KIND_TXT,
    SOURCE_CHANGED_WARNING,
    STRUCTURE_WARNING,
    TABLES_WARNING,
    DocumentError,
    LoadedDocument,
    _odt_text,
    _set_odt_text,
    kind_for,
    load_document,
    save_document,
    strip_heading_mark,
)

FILLER = ("Ein langer Absatz, der nur dazu dient, das Kapitel ueber die Mindestlaenge zu bringen, damit die "
          "Kapitelerkennung die Ueberschriften nicht als Rauschen verwirft. " * 2).strip()
TEXT_NS = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
OFFICE_NS = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TABLE_NS = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
ODT_CONTENT = """<?xml version="1.0" encoding="UTF-8"?>
<office:document-content xmlns:office="{office}" xmlns:text="{text}" xmlns:table="{table}" office:version="1.2">
 <office:body>
  <office:text>
   <text:sequence-decls><text:sequence-decl text:display-outline-level="0" text:name="Illustration"/></text:sequence-decls>
   <text:h text:style-name="Heading_20_1" text:outline-level="1">Kapitel Eins</text:h>
   <text:p text:style-name="Standard">Der <text:span text:style-name="T1">fette</text:span> Hund<text:s text:c="2"/>schlief.<text:line-break/>Zweite Zeile.</text:p>
   <text:p text:style-name="Standard">Teh cat<text:tab/>sat.</text:p>
   <text:p text:style-name="Standard">{filler}</text:p>
   <text:p text:style-name="Standard"> </text:p>
   <table:table table:name="T"><table:table-row><table:table-cell><text:p>In a cell.</text:p></table:table-cell></table:table-row></table:table>
   <text:list><text:list-item><text:p text:style-name="List">Listed item.</text:p></text:list-item></text:list>
   <text:h text:style-name="Heading_20_1" text:outline-level="1">Kapitel Zwei</text:h>
   <text:p text:style-name="Standard">{filler}</text:p>
   <text:p text:style-name="Standard">Ende gut.<text:note text:note-class="footnote"><text:note-body><text:p>Footnote text.</text:p></text:note-body></text:note></text:p>
  </office:text>
 </office:body>
</office:document-content>
""".format(office=OFFICE_NS, text=TEXT_NS, table=TABLE_NS, filler=FILLER)


def write_odt(path, content=ODT_CONTENT):
    with zipfile.ZipFile(str(path), "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/manifest.xml", "<manifest/>")
        archive.writestr("styles.xml", "<styles/>")
        archive.writestr("content.xml", content, compress_type=zipfile.ZIP_DEFLATED)
    return path


def odt_paragraphs(path):
    """Return the (tag, text, style) of every text:p / text:h under office:text, tables included."""
    with zipfile.ZipFile(str(path)) as archive:
        names = archive.namelist()
        root = ET.fromstring(archive.read("content.xml"))
        assert names[0] == "mimetype" and archive.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
    body = root.find("{%s}body/{%s}text" % (OFFICE_NS, OFFICE_NS))
    return [
        (element.tag.split("}")[1], _odt_text(element), element.get("{%s}style-name" % TEXT_NS))
        for element in body.iter() if element.tag in ("{%s}p" % TEXT_NS, "{%s}h" % TEXT_NS)
    ]


def make_docx(path, table=True):
    docx = pytest.importorskip("docx")
    document = docx.Document()
    document.add_paragraph("Chapter One", style="Heading 1")
    mixed = document.add_paragraph()
    mixed.add_run("Bold start").bold = True
    mixed.add_run(" and plain end.")
    document.add_paragraph("Teh cat sat on the mat.")
    document.add_paragraph(FILLER)
    document.add_paragraph("")  # spacing paragraph, not manuscript text
    if table:
        document.add_table(rows=1, cols=1).cell(0, 0).text = "In a cell."
    document.add_paragraph("Chapter Two", style="Heading 1")
    second = document.add_paragraph()
    second.add_run("Italic").italic = True
    second.add_run(" tail.")
    document.add_paragraph(FILLER)
    document.save(str(path))
    return path


# ------------------------------------------------------------ text formats
def test_kind_is_derived_from_the_suffix():
    assert kind_for("a.txt") == KIND_TXT and kind_for("A.TEXT") == KIND_TXT and kind_for("noext") == KIND_TXT
    assert kind_for("a.md") == KIND_MD and kind_for("a.markdown") == KIND_MD
    assert kind_for("a.docx") == KIND_DOCX and kind_for("a.odt") == KIND_ODT
    assert set(FORMATTED_KINDS) == {KIND_DOCX, KIND_ODT}


def test_text_and_markdown_load_as_is_with_line_refs(tmp_path):
    path = tmp_path / "book.md"
    path.write_text("# Title\n\nFirst para\nstill first.\n\n\nSecond.\n", encoding="utf-8")

    loaded = load_document(path)

    assert loaded.kind == KIND_MD
    assert loaded.text == "# Title\n\nFirst para\nstill first.\n\n\nSecond.\n"
    assert loaded.paragraph_texts() == ["# Title", "First para\nstill first.", "Second.\n"]
    assert [entry["ref"]["lines"] for entry in loaded.paragraphs] == [[1, 1], [3, 4], [7, 7]]
    assert loaded.source_path == str(path)

    out = save_document(loaded, "# Title\n\nChanged.\n", tmp_path / "out.md")
    assert out.read_text(encoding="utf-8") == "# Title\n\nChanged.\n"


def test_loaded_document_round_trips_through_dict():
    loaded = LoadedDocument("a\n\nb", KIND_ODT, [{"index": 0, "start": 0, "end": 1, "ref": {"p": 3, "heading": 0}}],
                            "x.odt", {"tables": 1})
    data = loaded.to_dict()
    restored = LoadedDocument.from_dict(data, text="a\n\nb", source_path="x.odt")
    assert restored == loaded
    assert LoadedDocument.from_dict(None).kind == KIND_TXT
    assert LoadedDocument.from_dict({"kind": "pdf", "paragraphs": [{"index": "x"}, 5]}).paragraphs == []


def test_heading_marks():
    assert strip_heading_mark("# Title") == (1, "Title")
    assert strip_heading_mark("### Deep") == (3, "Deep")
    assert strip_heading_mark("#NoSpace") == (0, "#NoSpace")
    assert strip_heading_mark("Plain") == (0, "Plain")


# ------------------------------------------------------------------- docx
def test_docx_loads_headings_as_markdown_lines_and_skips_tables(tmp_path):
    path = make_docx(tmp_path / "book.docx")

    loaded = load_document(path)

    assert loaded.kind == KIND_DOCX
    assert loaded.paragraph_texts() == [
        "# Chapter One", "Bold start and plain end.", "Teh cat sat on the mat.", FILLER, "# Chapter Two",
        "Italic tail.", FILLER,
    ]
    assert "In a cell." not in loaded.text
    # body paragraph indices skip the empty spacing paragraph (4); table cells are not body paragraphs
    assert [entry["ref"]["p"] for entry in loaded.paragraphs] == [0, 1, 2, 3, 5, 6, 7]
    assert [entry["ref"]["heading"] for entry in loaded.paragraphs] == [1, 0, 0, 0, 1, 0, 0]
    assert loaded.meta["tables"] == 1
    assert len(paragraphs(loaded.text)) == len(loaded.paragraphs)
    for entry, text in zip(loaded.paragraphs, loaded.paragraph_texts()):
        assert loaded.text[entry["start"]:entry["end"]] == text

    result = split_document(loaded.text)
    assert result.method == "headings:markdown"
    assert [chapter.title for chapter in result.chapters] == ["Chapter One", "Chapter Two"]


def test_docx_round_trip_replaces_only_changed_paragraphs(tmp_path):
    docx = pytest.importorskip("docx")
    path = make_docx(tmp_path / "book.docx")
    loaded = load_document(path)
    new_text = loaded.text.replace("Teh cat", "The cat")
    warnings = []

    out = save_document(loaded, new_text, tmp_path / "book-reviewed.docx", warnings.append)

    assert warnings == []
    document = docx.Document(str(out))
    texts = [paragraph.text for paragraph in document.paragraphs]
    assert texts == ["Chapter One", "Bold start and plain end.", "The cat sat on the mat.", FILLER, "",
                     "Chapter Two", "Italic tail.", FILLER]
    untouched = document.paragraphs[1]
    assert [run.bold for run in untouched.runs] == [True, None]  # both runs survived
    changed = document.paragraphs[2]
    assert len(changed.runs) == 1 and changed.runs[0].text == "The cat sat on the mat."
    assert document.paragraphs[0].style.name == "Heading 1"
    assert len(document.tables) == 1 and document.tables[0].cell(0, 0).text == "In a cell."
    assert load_document(out).text == new_text


def test_docx_changed_paragraph_keeps_first_run_formatting(tmp_path):
    docx = pytest.importorskip("docx")
    path = make_docx(tmp_path / "book.docx")
    loaded = load_document(path)

    out = save_document(loaded, loaded.text.replace("Bold start and plain end.", "Bold start, plain end."),
                        tmp_path / "out.docx")

    changed = docx.Document(str(out)).paragraphs[1]
    assert [(run.text, run.bold) for run in changed.runs] == [("Bold start, plain end.", True)]


def test_docx_paragraph_count_change_rebuilds_the_document_and_warns(tmp_path):
    docx = pytest.importorskip("docx")
    path = make_docx(tmp_path / "book.docx")
    loaded = load_document(path)
    new_text = loaded.text.replace("Teh cat sat on the mat.", "Teh cat sat.\n\nOn the mat.")
    warnings = []

    out = save_document(loaded, new_text, tmp_path / "out.docx", warnings.append)

    assert warnings == [STRUCTURE_WARNING, TABLES_WARNING]
    document = docx.Document(str(out))
    assert [paragraph.text for paragraph in document.paragraphs] == [
        "Chapter One", "Bold start and plain end.", "Teh cat sat.", "On the mat.", FILLER, "Chapter Two",
        "Italic tail.", FILLER,
    ]
    assert document.paragraphs[0].style.name == "Heading 1"
    assert document.paragraphs[5].style.name == "Heading 1"
    assert document.paragraphs[2].style.name == "Normal"
    assert document.tables == []
    assert load_document(out).text == new_text


def test_docx_source_changed_on_disk_falls_back_to_a_fresh_document(tmp_path):
    docx = pytest.importorskip("docx")
    path = make_docx(tmp_path / "book.docx")
    loaded = load_document(path)
    document = docx.Document(str(path))
    document.paragraphs[2].runs[0].text = "Rewritten meanwhile."
    document.save(str(path))
    warnings = []

    out = save_document(loaded, loaded.text.replace("Teh", "The"), tmp_path / "out.docx", warnings.append)

    assert warnings[0] == SOURCE_CHANGED_WARNING
    assert "The cat sat on the mat." in [paragraph.text for paragraph in docx.Document(str(out)).paragraphs]


def test_fresh_docx_from_plain_text_uses_heading_styles(tmp_path):
    docx = pytest.importorskip("docx")
    loaded = LoadedDocument("# One\n\nBody.\n\n## Two\n\nMore body.", KIND_TXT, [], "", {})
    warnings = []

    out = save_document(loaded, loaded.text, tmp_path / "new.docx", warnings.append)

    assert warnings == []
    document = docx.Document(str(out))
    assert [(p.text, p.style.name) for p in document.paragraphs] == [
        ("One", "Heading 1"), ("Body.", "Normal"), ("Two", "Heading 2"), ("More body.", "Normal")
    ]
    assert load_document(out).text == loaded.text


def test_missing_python_docx_gives_a_clear_error(tmp_path, monkeypatch):
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "docx" or name.startswith("docx."):
            raise ImportError("No module named docx")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    (tmp_path / "x.docx").write_bytes(b"")
    with pytest.raises(DocumentError) as excinfo:
        load_document(tmp_path / "x.docx")
    assert "python-docx" in str(excinfo.value)


# -------------------------------------------------------------------- odt
def test_odt_loads_paragraphs_in_order_with_whitespace_expanded(tmp_path):
    path = write_odt(tmp_path / "buch.odt")

    loaded = load_document(path)

    assert loaded.kind == KIND_ODT
    assert loaded.paragraph_texts() == [
        "# Kapitel Eins", "Der fette Hund  schlief.\nZweite Zeile.", "Teh cat\tsat.", FILLER, "Listed item.",
        "# Kapitel Zwei", FILLER, "Ende gut.",
    ]
    assert "In a cell." not in loaded.text and "Footnote text." not in loaded.text
    # document-order indices skip the blank paragraph (4); the table's paragraph is not walked at all
    assert [entry["ref"]["p"] for entry in loaded.paragraphs] == [0, 1, 2, 3, 5, 6, 7, 8]
    assert loaded.meta["tables"] == 1
    assert len(paragraphs(loaded.text)) == len(loaded.paragraphs)
    assert [chapter.title for chapter in split_document(loaded.text).chapters] == ["Kapitel Eins", "Kapitel Zwei"]


def test_odt_round_trip_keeps_untouched_paragraphs_and_tables(tmp_path):
    path = write_odt(tmp_path / "buch.odt")
    loaded = load_document(path)
    new_text = loaded.text.replace("Teh cat\tsat.", "The cat\tsat  down.")
    warnings = []

    out = save_document(loaded, new_text, tmp_path / "buch-reviewed.odt", warnings.append)

    assert warnings == []
    rows = odt_paragraphs(out)
    assert [text for _, text, _ in rows] == [
        "Kapitel Eins", "Der fette Hund  schlief.\nZweite Zeile.", "The cat\tsat  down.", FILLER, " ", "In a cell.",
        "Listed item.", "Kapitel Zwei", FILLER, "Ende gut.", "Footnote text.",
    ]
    with zipfile.ZipFile(str(out)) as archive:
        content = archive.read("content.xml").decode("utf-8")
        assert archive.read("styles.xml") == b"<styles/>"
    assert '<text:span text:style-name="T1">fette</text:span>' in content  # untouched paragraph kept its span
    assert 'sat <text:s text:c="1" />down.' in content and "<text:note" in content
    assert rows[2][2] == "Standard"
    assert load_document(out).text == new_text


def test_odt_paragraph_count_change_rebuilds_and_warns(tmp_path):
    path = write_odt(tmp_path / "buch.odt")
    loaded = load_document(path)
    new_text = loaded.text.replace("Ende gut.", "Ende gut.\n\nAlles gut.")
    warnings = []

    out = save_document(loaded, new_text, tmp_path / "out.odt", warnings.append)

    assert warnings == [STRUCTURE_WARNING, TABLES_WARNING]
    rows = odt_paragraphs(out)
    assert [(tag, text) for tag, text, _ in rows] == [
        ("h", "Kapitel Eins"), ("p", "Der fette Hund  schlief.\nZweite Zeile."), ("p", "Teh cat\tsat."),
        ("p", FILLER), ("p", "Listed item."), ("h", "Kapitel Zwei"), ("p", FILLER), ("p", "Ende gut."),
        ("p", "Alles gut."),
    ]
    assert rows[0][2] == "Heading_20_1" and rows[1][2] == "Standard"
    assert load_document(out).text == new_text


def test_odt_source_changed_on_disk_is_detected(tmp_path):
    path = write_odt(tmp_path / "buch.odt")
    loaded = load_document(path)
    write_odt(path, ODT_CONTENT.replace("Ende gut.", "Anders."))
    warnings = []

    save_document(loaded, loaded.text.replace("Teh", "The"), tmp_path / "out.odt", warnings.append)

    assert warnings[0] == SOURCE_CHANGED_WARNING


def test_fresh_odt_from_plain_text(tmp_path):
    loaded = LoadedDocument("# One\n\nBody line.\nSecond line.\n\nTabbed\tand  spaced.", KIND_TXT, [], "", {})

    out = save_document(loaded, loaded.text, tmp_path / "new.odt")

    assert [(tag, text) for tag, text, _ in odt_paragraphs(out)] == [
        ("h", "One"), ("p", "Body line.\nSecond line."), ("p", "Tabbed\tand  spaced."),
    ]
    with zipfile.ZipFile(str(out)) as archive:
        assert "META-INF/manifest.xml" in archive.namelist()
    assert load_document(out).text == loaded.text


def test_odt_text_encoding_round_trips_spaces_tabs_and_breaks():
    element = ET.Element("{%s}p" % TEXT_NS)
    text = "  lead\ttab   three\nbreak end  "
    _set_odt_text(element, text)
    assert _odt_text(element) == text
    assert element.find("{%s}s" % TEXT_NS).get("{%s}c" % TEXT_NS) == "2"


def test_damaged_odt_raises_document_error(tmp_path):
    path = tmp_path / "bad.odt"
    path.write_bytes(b"not a zip")
    with pytest.raises(DocumentError):
        load_document(path)
    write_odt(path, "<broken")
    with pytest.raises(DocumentError):
        load_document(path)
