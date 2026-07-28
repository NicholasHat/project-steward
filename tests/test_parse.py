from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from truth_engine.db.models import (
    Artifact,
    ArtifactContent,
    ProcessingState,
    Project,
    Stage,
    StageState,
    StageStatus,
    StructuredTable,
)
from truth_engine.parse.handlers._common import tables_as_text
from truth_engine.parse.registry import UnsupportedFormatError, get_handler
from truth_engine.parse.service import parse_artifact, parse_project
from truth_engine.parse.types import ParsedTable

from . import fixtures


def test_tables_as_text_bounds_rows_and_returns_none_when_empty() -> None:
    # A workbook with no tables at all (title only) is a zero-text artifact,
    # not a bare filename to embed.
    assert tables_as_text("empty.xlsx", [], max_rows_per_table=50, max_chars=99) is None

    # Column headers are useful topic signal even with no data rows, so a
    # header-only sheet still yields text (not None) — blank rows are skipped.
    headers_only = ParsedTable(source="S", table_schema=["pH"], rows=[{"pH": None}, {"pH": ""}])
    text = tables_as_text("hdr.xlsx", [headers_only], max_rows_per_table=50, max_chars=999)
    assert text is not None
    assert "Columns: pH" in text
    assert "None" not in text  # blank cells are skipped, never emitted as "None"

    # Row sampling is capped; the header line is always present.
    big = ParsedTable(source="S", table_schema=["a"], rows=[{"a": str(i)} for i in range(100)])
    text = tables_as_text("big.xlsx", [big], max_rows_per_table=3, max_chars=10_000)
    assert text is not None
    data_rows = [ln for ln in text.splitlines() if ln.isdigit()]
    assert data_rows == ["0", "1", "2"]  # only the first 3 of 100 rows sampled
    assert "Columns: a" in text


def _make_artifact(
    db_session: Session, project: Project, path: Path, *, content_hash: str = "h"
) -> Artifact:
    artifact = Artifact(
        id=uuid.uuid4(),
        project_id=project.id,
        content_hash=content_hash,
        current_path=str(path),
        original_filename=path.name,
        file_type=path.suffix.lstrip(".").lower(),
        size_bytes=path.stat().st_size,
        processing_state=ProcessingState.pending,
    )
    db_session.add(artifact)
    db_session.flush()
    return artifact


def _content(db_session: Session, artifact: Artifact) -> ArtifactContent:
    content = db_session.scalar(
        select(ArtifactContent).where(ArtifactContent.artifact_id == artifact.id)
    )
    assert content is not None
    return content


def _tables(db_session: Session, artifact: Artifact) -> list[StructuredTable]:
    return list(
        db_session.scalars(
            select(StructuredTable).where(StructuredTable.artifact_id == artifact.id)
        ).all()
    )


# --------------------------------------------------------------------------- #
# Per-format handlers                                                         #
# --------------------------------------------------------------------------- #
def test_parse_text(db_session: Session, project: Project, tmp_path: Path) -> None:
    path = tmp_path / "note.txt"
    path.write_text("Meeting notes: discussed the March 12 timeline.")
    artifact = _make_artifact(db_session, project, path)

    changed = parse_artifact(db_session, artifact)

    assert changed is True
    content = _content(db_session, artifact)
    assert "March 12" in content.raw_text
    assert content.parser_name == "text"
    assert artifact.processing_state == ProcessingState.parsed


def test_parse_pdf(db_session: Session, project: Project, tmp_path: Path) -> None:
    path = tmp_path / "report.pdf"
    fixtures.make_pdf(path, "Hello Truth Engine")
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    assert "Hello Truth Engine" in content.raw_text
    assert content.parser_name == "pdfplumber"
    assert content.structure["page_count"] == 1
    assert content.structure["likely_scanned"] is False


def test_parse_docx(db_session: Session, project: Project, tmp_path: Path) -> None:
    path = tmp_path / "notes.docx"
    fixtures.make_docx(path, "Project Kickoff", "We discussed scope and timeline.")
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    assert "We discussed scope and timeline." in content.raw_text
    assert "Project Kickoff" in content.structure["headings"]
    tables = _tables(db_session, artifact)
    assert len(tables) == 1
    assert tables[0].rows == [{"name": "widget", "value": "42"}]


def test_parse_pptx(db_session: Session, project: Project, tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    fixtures.make_pptx(path, "Q3 Update", "Progress on the experiment.")
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    assert "Progress on the experiment." in content.raw_text
    assert content.structure["slide_count"] == 1
    assert content.structure["slide_titles"] == ["Q3 Update"]


def test_parse_image_captures_dimensions_and_no_raw_text(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "whiteboard.png"
    fixtures.make_png(path, size=(32, 24))
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    assert content.raw_text is None  # no OCR/captioning in this increment
    assert content.embedded_metadata["width"] == 32
    assert content.embedded_metadata["height"] == 24
    assert content.embedded_metadata["format"] == "PNG"


def test_parse_unsupported_format_raises(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedFormatError):
        get_handler("exe")


# --------------------------------------------------------------------------- #
# Structured tables stay structured                                          #
# --------------------------------------------------------------------------- #
def test_parse_xlsx_stays_structured_not_flattened_to_prose(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "costs.xlsx"
    fixtures.make_xlsx(
        path,
        "Costs",
        [["item", "amount"], ["reagent A", 120], ["reagent B", 75]],
    )
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    # raw_text is a lossy projection for the analysis stages (embed/NER/date/
    # phase/direction), not the table flattened into prose: the source of truth
    # stays the StructuredTable, whose rows keep their *typed* values (int 120,
    # not "120") — that's what "not flattened to prose" actually guarantees.
    assert content.raw_text is not None
    assert "Costs" in content.raw_text  # sheet name + headers reach the embedder
    assert "item" in content.raw_text and "amount" in content.raw_text
    assert content.structure["sheet_names"] == ["Costs"]

    tables = _tables(db_session, artifact)
    assert len(tables) == 1
    assert tables[0].source == "Costs"
    assert tables[0].table_schema == ["item", "amount"]
    assert tables[0].rows == [
        {"item": "reagent A", "amount": 120},
        {"item": "reagent B", "amount": 75},
    ]


def test_parse_csv_stays_structured(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "collaborators.csv"
    path.write_text("name,group\nAlice,Synthesis\nBob,Analysis\n")
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    content = _content(db_session, artifact)
    # Lossy analysis projection present; the structured rows remain the record.
    assert content.raw_text is not None
    assert "name" in content.raw_text and "group" in content.raw_text
    tables = _tables(db_session, artifact)
    assert tables[0].rows == [
        {"name": "Alice", "group": "Synthesis"},
        {"name": "Bob", "group": "Analysis"},
    ]


def test_parse_csv_falls_back_to_latin1_on_non_utf8(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    # A CSV saved in latin-1 (e.g. "Genève") is not valid UTF-8; the handler
    # must fall back rather than fail the whole file.
    path = tmp_path / "sites.csv"
    path.write_bytes("city,lab\nGen\xe8ve,Curie\n".encode("latin-1"))
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)  # must not raise

    tables = _tables(db_session, artifact)
    assert tables[0].rows == [{"city": "Genève", "lab": "Curie"}]


def test_parse_xlsx_falls_back_to_formula_text_when_no_cached_values(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    # A workbook written programmatically has no *cached* formula values, so a
    # data_only read returns None for every computed cell and the sheet looks
    # empty. The handler must fall back to reading the formula text instead.
    path = tmp_path / "calc.xlsx"
    fixtures.make_xlsx(path, "Calc", [["x", "y"], ["=1+1", "=2+2"], ["=3+3", "=4+4"]])
    artifact = _make_artifact(db_session, project, path)

    parse_artifact(db_session, artifact)

    tables = _tables(db_session, artifact)
    assert len(tables[0].rows) == 2  # not silently empty
    assert tables[0].rows[0] == {"x": "=1+1", "y": "=2+2"}


def test_reparse_replaces_tables_wholesale(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "costs.xlsx"
    fixtures.make_xlsx(path, "Costs", [["item"], ["a"], ["b"], ["c"]])
    artifact = _make_artifact(db_session, project, path)
    parse_artifact(db_session, artifact)
    assert len(_tables(db_session, artifact)[0].rows) == 3

    fixtures.make_xlsx(path, "Costs", [["item"], ["a"]])
    artifact.content_hash = "different-hash"  # simulate a content change
    parse_artifact(db_session, artifact)

    tables = _tables(db_session, artifact)
    assert len(tables) == 1  # no leftover rows/tables from the previous parse
    assert len(tables[0].rows) == 1


# --------------------------------------------------------------------------- #
# StageState idempotency                                                      #
# --------------------------------------------------------------------------- #
def test_reparse_unchanged_artifact_is_a_noop(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    path = tmp_path / "note.txt"
    path.write_text("hello")
    artifact = _make_artifact(db_session, project, path)

    assert parse_artifact(db_session, artifact) is True
    assert parse_artifact(db_session, artifact) is False  # StageState.input_hash unchanged


def test_parse_project_skips_already_parsed_artifacts(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    for i in range(2):
        (tmp_path / f"note{i}.txt").write_text(f"content {i}")
    a1 = _make_artifact(db_session, project, tmp_path / "note0.txt", content_hash="h0")
    a2 = _make_artifact(db_session, project, tmp_path / "note1.txt", content_hash="h1")
    db_session.commit()

    first = parse_project(db_session, project.id)
    assert first.parsed == 2
    assert first.skipped == 0

    second = parse_project(db_session, project.id)
    assert second.parsed == 0
    assert second.skipped == 2

    for artifact in (a1, a2):
        state = db_session.scalar(
            select(StageState).where(
                StageState.artifact_id == artifact.id, StageState.stage == Stage.parse
            )
        )
        assert state is not None
        assert state.status == StageStatus.done
        assert state.input_hash == artifact.content_hash


def test_parse_records_error_without_aborting_batch(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    good = tmp_path / "good.txt"
    good.write_text("fine")
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"not actually a pdf")

    _make_artifact(db_session, project, good, content_hash="good-hash")
    bad_artifact = _make_artifact(db_session, project, bad, content_hash="bad-hash")
    db_session.commit()

    result = parse_project(db_session, project.id)

    assert result.parsed == 1
    assert len(result.errors) == 1
    assert result.errors[0][0].id == bad_artifact.id

    state = db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == bad_artifact.id, StageState.stage == Stage.parse
        )
    )
    assert state is not None
    assert state.status == StageStatus.error
    assert state.error
    assert bad_artifact.processing_state == ProcessingState.error


def test_parse_unsupported_format_recorded_as_skipped_not_error(
    db_session: Session, project: Project, tmp_path: Path
) -> None:
    # An unsupported format (a video) is retained, not a failure: it must be
    # recorded as skipped/unsupported, kept out of `errors`, and not abort the
    # rest of the batch.
    good = tmp_path / "good.txt"
    good.write_text("fine")
    movie = tmp_path / "clip.mov"
    movie.write_bytes(b"\x00\x00\x00\x18ftypqt  not really a movie")

    _make_artifact(db_session, project, good, content_hash="good-hash")
    mov_artifact = _make_artifact(db_session, project, movie, content_hash="mov-hash")
    db_session.commit()

    result = parse_project(db_session, project.id)

    assert result.parsed == 1
    assert result.errors == []
    assert len(result.unsupported) == 1
    assert result.unsupported[0][0].id == mov_artifact.id

    state = db_session.scalar(
        select(StageState).where(
            StageState.artifact_id == mov_artifact.id, StageState.stage == Stage.parse
        )
    )
    assert state is not None
    assert state.status == StageStatus.skipped
    assert state.error  # informational note, disambiguated by status=skipped
    assert mov_artifact.processing_state == ProcessingState.unsupported

    # Re-running parse leaves it settled (no repeated work, still skipped).
    again = parse_project(db_session, project.id)
    assert len(again.unsupported) == 0  # already settled, not re-attempted
    assert again.skipped == 2  # both good.txt and clip.mov are up-to-date now
