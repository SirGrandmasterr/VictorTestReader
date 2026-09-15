"""Load manuscripts from ``.txt``/``.md``/``.docx``/``.odt`` and write edited text back.

The text handed to chunking and the workflow is plain text exactly as it was
for ``.txt`` files: paragraphs joined with a blank line. A ``LoadedDocument``
remembers, per paragraph, where it came from (``ref``), so that
``save_document`` can put edited text back into the original file at
*paragraph granularity*: paragraphs whose text did not change keep every bit
of formatting; a changed paragraph keeps its paragraph style and the
character formatting of its first run, the rest of the paragraph's inline
formatting (bold words, links, footnotes) is replaced by the new plain text.
When the number of paragraphs changed (the model merged or split some) the
file is rebuilt from scratch with the paragraph styles of the nearest original
paragraphs and the caller is warned.

Headings become Markdown-style lines (``# Title``) in the text so that
``chunking`` still detects chapters; the marker is removed again on save.

``python-docx`` is an optional dependency and imported lazily; ``.odt`` files
are handled with ``zipfile`` and ``xml.etree`` from the standard library.
"""

import io
import os
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from xml.etree import ElementTree as ET

from .chunking import _PARAGRAPH_BREAK, paragraphs as split_paragraphs, read_text_file

KIND_TXT = "txt"
KIND_MD = "md"
KIND_DOCX = "docx"
KIND_ODT = "odt"
KINDS = (KIND_TXT, KIND_MD, KIND_DOCX, KIND_ODT)
FORMATTED_KINDS = (KIND_DOCX, KIND_ODT)
SUFFIX_KINDS = {
    ".txt": KIND_TXT, ".text": KIND_TXT, "": KIND_TXT,
    ".md": KIND_MD, ".markdown": KIND_MD,
    ".docx": KIND_DOCX,
    ".odt": KIND_ODT,
}
KIND_SUFFIXES = {KIND_TXT: ".txt", KIND_MD: ".md", KIND_DOCX: ".docx", KIND_ODT: ".odt"}
KIND_LABELS = {KIND_TXT: "Text", KIND_MD: "Markdown", KIND_DOCX: "Word document", KIND_ODT: "OpenDocument text"}
MANUSCRIPT_PATTERNS = "*.txt *.md *.text *.markdown *.docx *.odt"
DOCX_PACKAGE = "python-docx"

STRUCTURE_WARNING = "Paragraph structure changed; inline formatting could not be preserved"
TABLES_WARNING = "Tables of the original document were dropped because the paragraph structure changed"
SOURCE_CHANGED_WARNING = "The original file changed since it was loaded; a fresh document was written"
SOURCE_MISSING_WARNING = "The original file is missing; a fresh document without its styles was written"
FORMATTING_NOTE = (
    "Unchanged paragraphs keep their formatting. In a changed paragraph the paragraph style and the "
    "formatting of its first run are kept; bold or italic words, links and footnotes inside it are lost."
)

_HEADING_MARK = re.compile(r"^(#{1,6}) ")
_ODT_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "manifest": "urn:oasis:names:tc:opendocument:xmlns:manifest:1.0",
}
_ODT_MIMETYPE = "application/vnd.oasis.opendocument.text"


def _odt(prefix, name):
    return "{{{0}}}{1}".format(_ODT_NS[prefix], name)


_ODT_P = _odt("text", "p")
_ODT_H = _odt("text", "h")
_ODT_CONTAINERS = {_odt("text", "section"), _odt("text", "list"), _odt("text", "list-item"),
                   _odt("text", "list-header")}
_ODT_SKIPPED_INLINE = {_odt("text", "note"), _odt("office", "annotation"), _odt("text", "soft-page-break"),
                       _odt("office", "annotation-end")}
_ODT_DECLS = ("-decls", "forms", "tracked-changes")


class DocumentError(RuntimeError):
    """A manuscript could not be read or written (unsupported, damaged or missing dependency)."""


# ------------------------------------------------------------------ model
@dataclass
class LoadedDocument:
    """A manuscript as plain text plus the map back to its source paragraphs.

    ``paragraphs`` is a list of ``{"index": n, "start": s, "end": e, "ref": locator}``
    where ``text[s:e]`` is the paragraph and ``ref`` is format-specific:
    ``{"p": n, "heading": level}`` (paragraph index in the .docx body / the
    .odt document order) or ``{"lines": [first, last]}`` for text formats.
    """

    text: str
    kind: str = KIND_TXT
    paragraphs: list = field(default_factory=list)
    source_path: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def formatted(self):
        """Whether the source keeps formatting that a plain-text write would lose."""
        return self.kind in FORMATTED_KINDS

    @property
    def suffix(self):
        return KIND_SUFFIXES.get(self.kind, ".txt")

    def paragraph_texts(self):
        return [self.text[entry["start"]:entry["end"]] for entry in self.paragraphs]

    def to_dict(self):
        """The persisted part (the text itself lives in the project's chapters)."""
        return {"kind": self.kind, "paragraphs": [dict(entry) for entry in self.paragraphs], "meta": dict(self.meta)}

    @classmethod
    def from_dict(cls, data, text="", source_path=""):
        data = data or {}
        kind = data.get("kind") if data.get("kind") in KINDS else KIND_TXT
        entries = []
        for entry in data.get("paragraphs") or []:
            try:
                entries.append({"index": int(entry["index"]), "start": int(entry["start"]),
                                "end": int(entry["end"]), "ref": dict(entry.get("ref") or {})})
            except (KeyError, TypeError, ValueError):
                continue
        return cls(text, kind, entries, source_path, dict(data.get("meta") or {}))


def kind_for(path):
    """Return the document kind for a file name (unknown suffixes are plain text)."""
    return SUFFIX_KINDS.get(Path(str(path)).suffix.lower(), KIND_TXT)


def strip_heading_mark(text):
    """Return ``(level, title)`` for a ``# Title`` line, ``(0, text)`` otherwise."""
    match = _HEADING_MARK.match(text)
    if not match:
        return 0, text
    return len(match.group(1)), text[match.end():]


def _clean_paragraph(text):
    """Normalise a source paragraph so that re-splitting the joined text finds it again."""
    return _PARAGRAPH_BREAK.sub("\n", text.replace("\r\n", "\n").replace("\r", "\n")).strip("\n")


def _text_paragraphs(text):
    """Split plain text into the same paragraphs ``chunking`` uses (blank-line separated)."""
    return [paragraph for paragraph, _ in split_paragraphs(text)]


def _assemble(blocks, kind, source_path, meta):
    """Join ``(text, ref)`` blocks with blank lines into a LoadedDocument."""
    texts = []
    entries = []
    position = 0
    for index, (text, ref) in enumerate(blocks):
        if texts:
            position += 2
        entries.append({"index": index, "start": position, "end": position + len(text), "ref": ref})
        texts.append(text)
        position += len(text)
    return LoadedDocument("\n\n".join(texts), kind, entries, str(source_path), meta)


# ---------------------------------------------------------------- loading
def load_document(path):
    """Read a manuscript; the returned text is ready for ``chunking.split_document``."""
    path = Path(path)
    kind = kind_for(path)
    if kind == KIND_DOCX:
        return _load_docx(path)
    if kind == KIND_ODT:
        return _load_odt(path)
    text = read_text_file(path)
    return _load_text(text, kind, path)


def text_document(text, path=""):
    """Wrap in-memory text as a LoadedDocument (kind from the suffix; formatted kinds become text)."""
    kind = kind_for(path) if path else KIND_TXT
    return _load_text(text, kind if kind not in FORMATTED_KINDS else KIND_TXT, path)


def _load_text(text, kind, path):
    entries = []
    position = 0
    for index, (paragraph, separator) in enumerate(split_paragraphs(text)):
        end = position + len(paragraph)
        first = 1 + text[:position].count("\n")
        last = 1 + text[:max(position, end - 1)].count("\n")
        entries.append({"index": index, "start": position, "end": end, "ref": {"lines": [first, last]}})
        position = end + len(separator)
    return LoadedDocument(text, kind, entries, str(path), {})


def _import_docx():
    try:
        import docx  # noqa: F401  (optional dependency)
    except ImportError as exc:
        raise DocumentError(
            "Reading and writing .docx files needs the {0} package: pip install \"{0}>=1.1,<2\"".format(DOCX_PACKAGE)
        ) from exc
    return docx


def _docx_heading_level(paragraph):
    """Heading level 1–6 of a python-docx paragraph, 0 for body text."""
    try:
        name = paragraph.style.name if paragraph.style is not None else ""
    except Exception:  # a damaged style reference must not stop the import
        name = ""
    name = (name or "").strip().lower()
    if name == "title":
        return 1
    match = re.match(r"heading\s*(\d)", name)
    if match:
        return max(1, min(6, int(match.group(1))))
    return 0


def _load_docx(path):
    docx = _import_docx()
    try:
        document = docx.Document(str(path))
    except Exception as exc:
        raise DocumentError("The Word document could not be read: {0}".format(exc)) from exc
    blocks = []
    for index, paragraph in enumerate(document.paragraphs):
        raw = paragraph.text or ""
        if not raw.strip():
            continue
        level = _docx_heading_level(paragraph)
        text = _clean_paragraph(raw)
        if level:
            text = "#" * level + " " + text
        blocks.append((text, {"p": index, "heading": level}))
    meta = {"tables": len(document.tables), "body_paragraphs": len(document.paragraphs)}
    return _assemble(blocks, KIND_DOCX, path, meta)


def _odt_paragraph_elements(container):
    """Yield text:h / text:p elements in document order, skipping tables and frames."""
    for child in container:
        if child.tag in (_ODT_P, _ODT_H):
            yield child
        elif child.tag in _ODT_CONTAINERS:
            for element in _odt_paragraph_elements(child):
                yield element


def _odt_text(element):
    """Text of one paragraph with text:s / text:tab / text:line-break expanded."""
    parts = [element.text or ""]
    for child in element:
        tag = child.tag
        if tag == _odt("text", "s"):
            try:
                parts.append(" " * max(1, int(child.get(_odt("text", "c"), "1"))))
            except ValueError:
                parts.append(" ")
        elif tag == _odt("text", "tab"):
            parts.append("\t")
        elif tag == _odt("text", "line-break"):
            parts.append("\n")
        elif tag in _ODT_SKIPPED_INLINE:
            pass  # footnote bodies and comments are not manuscript text
        else:
            parts.append(_odt_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _odt_heading_level(element):
    if element.tag != _ODT_H:
        return 0
    try:
        return max(1, min(6, int(element.get(_odt("text", "outline-level"), "1"))))
    except ValueError:
        return 1


def _read_odt(path):
    """Return ``(entries, content_root, namespaces)`` of an .odt package."""
    try:
        with zipfile.ZipFile(str(path)) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
    except (zipfile.BadZipFile, OSError, KeyError) as exc:
        raise DocumentError("The OpenDocument file could not be read: {0}".format(exc)) from exc
    if "content.xml" not in entries:
        raise DocumentError("The OpenDocument file has no content.xml.")
    try:
        namespaces = [(prefix, uri) for _, (prefix, uri) in
                      ET.iterparse(io.BytesIO(entries["content.xml"]), events=("start-ns",))]
        root = ET.fromstring(entries["content.xml"])
    except ET.ParseError as exc:
        raise DocumentError("The OpenDocument content is not valid XML: {0}".format(exc)) from exc
    return entries, root, namespaces


def _odt_body(root):
    body = root.find("office:body/office:text", _ODT_NS)
    if body is None:
        raise DocumentError("The OpenDocument file is not a text document.")
    return body


def _load_odt(path):
    _, root, _ = _read_odt(path)
    body = _odt_body(root)
    blocks = []
    for index, element in enumerate(_odt_paragraph_elements(body)):
        raw = _odt_text(element)
        if not raw.strip():
            continue
        level = _odt_heading_level(element)
        text = _clean_paragraph(raw)
        if level:
            text = "#" * level + " " + text
        blocks.append((text, {"p": index, "heading": level}))
    tables = len(body.findall(".//table:table", _ODT_NS))
    return _assemble(blocks, KIND_ODT, path, {"tables": tables})


# ----------------------------------------------------------------- saving
def save_document(loaded, new_text, out_path, on_warning=None):
    """Write ``new_text`` to ``out_path`` in the format its suffix asks for.

    For ``.docx``/``.odt`` targets that come from a loaded document of the same
    kind the original file is updated paragraph by paragraph (see the module
    docstring); otherwise a fresh document is written. ``on_warning`` receives
    one message per limitation hit. Returns the written path.
    """
    out_path = Path(out_path)
    target = kind_for(out_path)
    warn = on_warning or (lambda message: None)
    if target == KIND_DOCX:
        _save_docx(loaded, new_text, out_path, warn)
    elif target == KIND_ODT:
        _save_odt(loaded, new_text, out_path, warn)
    else:
        out_path.write_text(new_text, encoding="utf-8")
    return out_path


def _heading_text(text, level):
    """Strip the Markdown marker a heading got on load (when it still carries one)."""
    marked, title = strip_heading_mark(text)
    return title if marked and level else text


def _nearest_index(index, new_count, headings, heading):
    """Index of the original paragraph closest to ``index`` by relative position.

    ``headings`` says which originals were headings; a heading only borrows
    from a heading and body text only from body text, so a paragraph that
    lands next to a chapter title does not inherit the title's style.
    """
    old_count = len(headings)
    if old_count <= 0:
        return None
    nearest = 0 if new_count <= 1 else int(round(index * (old_count - 1) / float(new_count - 1)))
    if bool(headings[nearest]) == bool(heading):
        return nearest
    for distance in range(1, old_count):
        for candidate in (nearest - distance, nearest + distance):
            if 0 <= candidate < old_count and bool(headings[candidate]) == bool(heading):
                return candidate
    return nearest


def _round_trip_plan(loaded, new_text, target):
    """Return ``(old_texts, new_texts, round_trip)``; round_trip is False when a fresh file is needed."""
    old_texts = loaded.paragraph_texts()
    new_texts = [paragraph for paragraph in _text_paragraphs(new_text) if paragraph.strip()]
    source = Path(loaded.source_path) if loaded.source_path else None
    round_trip = (
        loaded.kind == target and source is not None and source.is_file()
        and len(old_texts) == len(loaded.paragraphs) and len(new_texts) == len(old_texts)
    )
    return old_texts, new_texts, round_trip


def _docx_style(document, name):
    try:
        return document.styles[name]
    except KeyError:
        return None


def _set_docx_paragraph_text(paragraph, text):
    """Replace the paragraph content keeping its properties and the first run's formatting."""
    from docx.oxml.ns import qn
    from docx.text.run import Run

    element = paragraph._p
    first_run = None
    for child in list(element):
        if child.tag == qn("w:pPr"):
            continue
        if child.tag == qn("w:r") and first_run is None:
            first_run = child
            continue
        element.remove(child)
    if first_run is None:
        paragraph.add_run(text)
    else:
        Run(first_run, paragraph).text = text


def _save_docx(loaded, new_text, out_path, warn):
    docx = _import_docx()
    old_texts, new_texts, round_trip = _round_trip_plan(loaded, new_text, KIND_DOCX)
    document = None
    if loaded.kind == KIND_DOCX and loaded.source_path and Path(loaded.source_path).is_file():
        try:
            document = docx.Document(loaded.source_path)
        except Exception as exc:
            raise DocumentError("The original Word document could not be read: {0}".format(exc)) from exc
    elif loaded.kind == KIND_DOCX and old_texts:
        warn(SOURCE_MISSING_WARNING)
    if round_trip:
        body = document.paragraphs
        refs = [entry["ref"] for entry in loaded.paragraphs]
        if _docx_source_changed(body, refs, old_texts):
            warn(SOURCE_CHANGED_WARNING)
        else:
            for ref, old, new in zip(refs, old_texts, new_texts):
                if new != old:
                    _set_docx_paragraph_text(body[ref["p"]], _heading_text(new, ref.get("heading", 0)))
            _write_docx(document, out_path)
            return
    if document is not None:
        if old_texts:
            warn(STRUCTURE_WARNING)
            if loaded.meta.get("tables"):
                warn(TABLES_WARNING)
        _rebuild_docx(document, loaded, new_texts)
    else:
        document = docx.Document()
        _fill_docx(document, loaded, new_texts, styles=None)
    _write_docx(document, out_path)


def _docx_source_changed(body, refs, old_texts):
    """Whether the referenced paragraphs no longer carry the text that was loaded."""
    if any(ref.get("p", -1) >= len(body) for ref in refs):
        return True
    return any(
        _clean_paragraph(body[ref["p"]].text or "") != _heading_text(old, ref.get("heading", 0))
        for ref, old in zip(refs, old_texts)
    )


def _write_docx(document, out_path):
    try:
        document.save(str(out_path))
    except OSError as exc:
        raise DocumentError("The Word document could not be written: {0}".format(exc)) from exc


def _rebuild_docx(document, loaded, new_texts):
    """Empty the body (tables included) and refill it, keeping the section properties."""
    from docx.oxml.ns import qn

    styles = []
    for entry in loaded.paragraphs:
        index = entry["ref"].get("p", -1)
        try:
            styles.append(document.paragraphs[index].style)
        except (IndexError, KeyError):
            styles.append(None)
    body = document.element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)
    _fill_docx(document, loaded, new_texts, styles)


def _fill_docx(document, loaded, new_texts, styles):
    """Append paragraphs; headings get the matching style, the rest the nearest original's."""
    headings = [entry["ref"].get("heading", 0) for entry in loaded.paragraphs][:len(styles or [])]
    for index, text in enumerate(new_texts):
        level, title = strip_heading_mark(text)
        style = _docx_style(document, "Heading {0}".format(level)) if level else None
        if style is not None:
            text = title
        elif styles:
            nearest = _nearest_index(index, len(new_texts), headings, level)
            style = styles[nearest]
            if level and headings[nearest]:
                text = title  # the nearest original was a heading: its style carries the level
        paragraph = document.add_paragraph(text)
        if style is not None:
            try:
                paragraph.style = style
            except (KeyError, ValueError):
                pass


def _set_odt_text(element, text):
    """Replace an element's content with ``text`` (attributes stay), encoding spaces, tabs and breaks."""
    for child in list(element):
        element.remove(child)
    element.text = None
    last = None

    def append(tag, **attributes):
        nonlocal last
        last = ET.SubElement(element, tag)
        for key, value in attributes.items():
            last.set(key, value)
        return last

    def emit(chunk):
        nonlocal last
        if not chunk:
            return
        if last is None:
            element.text = (element.text or "") + chunk
        else:
            last.tail = (last.tail or "") + chunk

    for line_number, line in enumerate(text.split("\n")):
        if line_number:
            append(_odt("text", "line-break"))
        for piece_number, piece in enumerate(line.split("\t")):
            if piece_number:
                append(_odt("text", "tab"))
            position = 0
            for match in re.finditer(r" {2,}|^ ", piece):
                emit(piece[position:match.start()])
                count = len(match.group(0))
                if match.start() == 0:
                    append(_odt("text", "s"), **{_odt("text", "c"): str(count)})
                else:
                    emit(" ")
                    if count > 1:
                        append(_odt("text", "s"), **{_odt("text", "c"): str(count - 1)})
                position = match.end()
            emit(piece[position:])


def _serialise_odt_content(root, namespaces):
    for prefix, uri in namespaces:
        if prefix and not re.fullmatch(r"ns\d+", prefix):
            try:
                ET.register_namespace(prefix, uri)
            except ValueError:
                pass
    return b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="utf-8")


def _write_odt_package(entries, content, out_path):
    """Write an .odt zip: ``mimetype`` first and stored, ``content.xml`` replaced."""
    out_path = Path(out_path)
    temporary = out_path.with_name(out_path.name + ".tmp")
    try:
        with zipfile.ZipFile(str(temporary), "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", entries.get("mimetype", _ODT_MIMETYPE.encode("ascii")),
                             compress_type=zipfile.ZIP_STORED)
            for name, data in entries.items():
                if name in ("mimetype", "content.xml"):
                    continue
                archive.writestr(name, data)
            archive.writestr("content.xml", content)
        os.replace(str(temporary), str(out_path))
    except OSError as exc:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise DocumentError("The OpenDocument file could not be written: {0}".format(exc)) from exc


_ODT_MANIFEST = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<manifest:manifest xmlns:manifest="{0}" manifest:version="1.2">\n'
    ' <manifest:file-entry manifest:full-path="/" manifest:version="1.2" manifest:media-type="{1}"/>\n'
    ' <manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>\n'
    '</manifest:manifest>\n'
).format(_ODT_NS["manifest"], _ODT_MIMETYPE)
_ODT_EMPTY_CONTENT = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<office:document-content xmlns:office="{office}" xmlns:text="{text}" xmlns:table="{table}" '
    'xmlns:style="{style}" office:version="1.2">'
    '<office:body><office:text/></office:body></office:document-content>'
).format(**_ODT_NS)


def _save_odt(loaded, new_text, out_path, warn):
    old_texts, new_texts, round_trip = _round_trip_plan(loaded, new_text, KIND_ODT)
    entries = root = None
    namespaces = []
    if loaded.kind == KIND_ODT and loaded.source_path and Path(loaded.source_path).is_file():
        entries, root, namespaces = _read_odt(loaded.source_path)
    elif loaded.kind == KIND_ODT and old_texts:
        warn(SOURCE_MISSING_WARNING)
    if root is None:
        entries = {"mimetype": _ODT_MIMETYPE.encode("ascii"),
                   "META-INF/manifest.xml": _ODT_MANIFEST.encode("utf-8")}
        root = ET.fromstring(_ODT_EMPTY_CONTENT)
        namespaces = list(_ODT_NS.items())
    body = _odt_body(root)
    elements = list(_odt_paragraph_elements(body))
    if round_trip:
        refs = [entry["ref"] for entry in loaded.paragraphs]
        if _odt_source_changed(elements, refs, old_texts):
            warn(SOURCE_CHANGED_WARNING)
        else:
            for ref, old, new in zip(refs, old_texts, new_texts):
                if new != old:
                    _set_odt_text(elements[ref["p"]], _heading_text(new, ref.get("heading", 0)))
            _write_odt_package(entries, _serialise_odt_content(root, namespaces), out_path)
            return
    if elements and old_texts:
        warn(STRUCTURE_WARNING)
        if loaded.meta.get("tables"):
            warn(TABLES_WARNING)
    _rebuild_odt(body, elements, loaded, new_texts)
    _write_odt_package(entries, _serialise_odt_content(root, namespaces), out_path)


def _odt_source_changed(elements, refs, old_texts):
    if any(ref.get("p", -1) >= len(elements) for ref in refs):
        return True
    return any(
        _clean_paragraph(_odt_text(elements[ref["p"]])) != _heading_text(old, ref.get("heading", 0))
        for ref, old in zip(refs, old_texts)
    )


def _rebuild_odt(body, elements, loaded, new_texts):
    """Empty office:text (declarations stay) and refill it with plain paragraphs."""
    style_attr = _odt("text", "style-name")
    templates = []
    for entry in loaded.paragraphs:
        index = entry["ref"].get("p", -1)
        element = elements[index] if 0 <= index < len(elements) else None
        if element is None:
            templates.append((_ODT_P, None))
        else:
            templates.append((element.tag, element.get(style_attr)))
    for child in list(body):
        if any(child.tag.endswith(suffix) for suffix in _ODT_DECLS):
            continue
        body.remove(child)
    headings = [tag == _ODT_H for tag, _ in templates]
    for index, text in enumerate(new_texts):
        level, title = strip_heading_mark(text)
        nearest = _nearest_index(index, len(new_texts), headings, level)
        tag, style = templates[nearest] if nearest is not None else (_ODT_P, None)
        if level:
            element = ET.SubElement(body, _ODT_H)
            element.set(_odt("text", "outline-level"), str(level))
            text = title
        else:
            element = ET.SubElement(body, _ODT_P)
        if style and tag == element.tag:
            element.set(style_attr, style)
        _set_odt_text(element, text)
