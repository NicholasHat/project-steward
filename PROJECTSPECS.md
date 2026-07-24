# Project Spec: Project Truth Engine (working title)

## Instructions for Claude Code

You are being handed a product/technical specification, not code. Your task is to produce a **full implementation plan**, not to start writing application code yet. Specifically, produce:

1. A proposed system architecture (components, data flow, storage layers).
2. A recommended tech stack with rationale (language, embedding store, database, parsing libraries, orchestration approach), optimized for a solo/small-team build that needs to ship an MVP before it's "correct."
3. A concrete data schema for every store described below (raw files, structured metadata, entities, embeddings, timeline events, phase assignments, relationship graph, confidence scores, version/audit history).
4. A module-by-module breakdown of the pipeline (steps 1–12 below), each with: inputs, outputs, and the specific failure modes to design against.
5. A phased build plan (MVP → v1 → v2) that sequences the pipeline so there's a usable product at each stage, not just at the end.
6. A list of open technical risks or ambiguous decisions you think need to be resolved before or during implementation, flagged explicitly rather than silently resolved.

Do not assume a specific vertical beyond what's stated below (R&D/chemical research labs as the initial wedge). Do not over-engineer for scale beyond a single project's worth of files (hundreds to low thousands of documents) in the MVP.

---

## 1. Product Vision

A system that ingests a messy, unstructured folder of project artifacts (PDFs, spreadsheets, notes, meeting minutes, slideshows, images) — typically confusingly named and reflecting a project whose direction has shifted over time — and reconstructs a coherent, trustworthy picture of the project: what actually happened, in what order, what's missing, and what the project's *current* direction actually is (as opposed to what its earliest artifacts imply). The system keeps this picture usable and current as new artifacts are added, rather than being a one-time cleanup pass.

**Core promise to the user, expressed as three product surfaces:**

1. **A way to view all artifacts** — a browsable, organized index of every file in the project, with clean names and structure, without touching the underlying raw files.
2. **A timeline with gaps surfaced** — a reconstructed chronology of the project's actual work, with explicitly flagged missing phases or expected-but-absent artifacts.
3. **Artifacts contextualized against the current goal** — every artifact is evaluated against the project's *most recently inferred* direction, and flagged as current, superseded/outdated ("vision drift"), or unclear — not just sorted by date.

**Initial target vertical:** R&D / scientific research projects (e.g., a chemical research lab folder containing research paper PDFs, progress-update slideshows, meeting notes, personal research notes, brainstormed solution ideas, tooling/software mentions, cost data, collaborator/group mentions, and program-generated spreadsheets). Build for this vertical specifically first; design the phase-model and entity-extraction layers so a second vertical (e.g. software/engineering projects) can be added later without a rewrite.

---

## 2. Pipeline Overview

```
1. Ingest project (all files)
2. Parse content deterministically (no raw dump into an LLM context window)
3. Extract metadata and entities
4. Store raw text + structured fields
5. Generate embeddings (chunk-level + doc-level summaries)
6. Build a timeline from multi-signal date resolution + content-described events
7. Detect a project's field/domain, then apply the matching phase model
8. Compare embedding clusters over time / across phases to detect direction and drift
9. Cross-reference recency + citation graph to infer current direction and flag outdated/superseded artifacts — with a human confirmation checkpoint
10. Detect missing phases or expected-but-absent artifacts against the phase model
11. Generate an organized, renamed VIEW of the project (non-destructive)
12. Generate a self-updating project report
13. Dashboard: browse all artifacts, timeline, direction, gaps, report
```

Steps 2–5 should be almost entirely deterministic/rule-based or use small specialized models (parsers, OCR, NER, embedding models) — not a general-purpose LLM reading raw file contents into its context window. LLM reasoning is reserved for judgment calls: direction inference, drift/staleness labeling, gap detection, renaming suggestions, and report synthesis (steps 7 partially, 9, 10, 11, 12). This keeps cost bounded, keeps the system auditable (you can point to *why* a date or entity was extracted), and avoids garbage-in/garbage-out from dumping hundreds of raw documents into one prompt.

---

## 3. Detailed Design Decisions

### 3.1 Ingestion & Deterministic Parsing (Steps 1–4)

- Support at minimum: PDF (text + scanned/OCR), DOCX, XLSX/CSV, PPTX, common image formats (with OCR/vision-model captioning for diagrams/photos/whiteboards), and plain text/markdown notes.
- Each file gets a stable internal ID independent of filename or path — filenames and paths are mutable presentation, not identity.
- Deterministic parsing extracts: raw text, document structure (headings, slide titles, sheet/tab names, table contents), file-level metadata (created/modified timestamps, author if present, file type), and any embedded metadata (EXIF for images, document properties for Office files).
- Structured fields (from spreadsheets/tables especially) should be stored as structured data, not flattened into prose — a cost spreadsheet should remain queryable as a spreadsheet, not just be summarized.

### 3.2 Entity & Date Extraction — Multi-Signal, Confidence-Scored (Step 6 groundwork)

Timestamps are the load-bearing input for the entire timeline/drift/gap pipeline, so treat date resolution as its own subsystem rather than a single deterministic lookup:

- **Signal 1 (highest trust): content-embedded dates and date-implying references** extracted via NER/date-parsing from the actual text — "as discussed in our March 12 meeting," "results from last week's run," explicit dates in meeting notes or slide titles.
- **Signal 2 (medium trust): document-internal metadata timestamps** — creation/modified dates embedded in the file format itself (e.g., Office document properties, PDF metadata), which are more reliable than filesystem dates but still get altered by exports/conversions.
- **Signal 3 (lowest trust, still used): filesystem metadata** (OS-level created/modified timestamps) — used as a fallback and as a cross-check, not as ground truth, since copies/syncs/migrations routinely corrupt this.
- Each artifact's resolved date should carry a **confidence score and a source label** (e.g., "high confidence, from content" vs. "low confidence, filesystem only"), and the UI/timeline should visibly distinguish confident placements from inferred/uncertain ones rather than presenting a single flat chronology as ground truth.
- Also extract non-date entities: people/collaborator names, group/team mentions, tools/software mentioned, cost figures, named experiments or hypotheses, referenced external sources (e.g., citations to research papers). These feed both the timeline (events) and the relationship graph (3.4).

### 3.3 Field/Domain Detection → Phase Model Selection (Step 7)

Two-stage approach, as discussed:

1. **Domain classification first.** Using the corpus as a whole (not a single artifact), classify the project's general field — e.g., scientific/research project, software/engineering project, business/marketing project, creative project, etc. This should be a coarse, small classification task, ideally with an explicit confidence score and the option for the user to confirm/override it.
2. **Apply a general phase-model template matching the detected domain**, rather than inferring phases from scratch per project (too hallucination-prone with sparse early data). Maintain a small library of general phase templates, e.g.:
   - **Engineering project:** Initiation → Planning & Design → Execution → Monitoring & Control → Closure.
   - **Research/scientific project:** Conceptualization & Planning → Execution (experimentation/data collection) → Analysis → Reporting & Dissemination.
   - (Design the template library so additional domains can be added as config/data, not code changes.)
3. Map each ingested artifact to its most likely phase(s) in the selected template (an artifact can map to more than one phase, e.g., a meeting note that spans planning and execution discussion).
4. If domain confidence is low or the corpus doesn't fit any template well, fall back to a generic minimal phase model (e.g., Start → Middle → Recent) rather than forcing a bad fit — and flag this to the user rather than silently guessing.

### 3.4 Direction Inference & "Vision Drift" Detection (Steps 8–9)

This is the system's core differentiator — don't just use recency to decide what's "current."

- **Signal A — embedding cluster drift over time:** compute doc-level embeddings, cluster them, and track how clusters shift across the resolved timeline/phases. A meaningful shift in cluster centroid/topic over time is evidence of a direction change, distinct from mere accumulation of similar documents.
- **Signal B — citation/reference graph:** build a lightweight graph of which artifacts reference, build on, or are mentioned by later artifacts (e.g., a slide deck that explicitly references an earlier research note; a spreadsheet whose outputs get discussed in later meeting notes). Artifacts that continue to be referenced/built upon are "adopted" into the current direction; artifacts that are never referenced again after a point are candidate dead-ends — this is a more robust signal than raw recency and guards against a single stray recent document skewing the whole inferred direction.
- **Combine Signal A + Signal B (+ resolved timeline position)** to produce, for each artifact, a label: *current / superseded (vision drift) / unclear*, each with a stated rationale the user can inspect (not just a black-box tag).
- **Human confirmation checkpoint:** before any renaming, reorganization, or report generation acts on these labels, present the inferred direction and the flagged-as-outdated artifacts to the user for confirmation or correction. Do not let a single automated inference pass unilaterally decide what's "corrupted by vision drift" and then act on that irreversibly downstream.

### 3.5 Gap Detection (Step 10)

- Using the phase model selected in 3.3, check for phases with no or very few mapped artifacts, and flag them as candidate gaps ("no artifacts detected for Analysis phase").
- Also detect content-level gaps: places where the text explicitly references a future or pending step ("we still need to run the control experiment," "cost estimate TBD," "waiting on results from Group X") that never gets a corresponding later artifact.
- Distinguish between "structurally missing" (no phase coverage) and "explicitly promised but unfulfilled" (referenced in text but never delivered) — these are different confidence levels of gap and should be presented differently.

### 3.6 Non-Destructive View/Index Layer (Step 11)

- **Never mutate or move original files.** This matters generally, but especially for the R&D/lab vertical, where data integrity, audit trails, and (in some settings) regulatory/chain-of-custody expectations make silent renaming or reorganization a real problem, not just a nicety.
- The organized names/structure/folders the system produces are a **projection/index over the raw files**, stored as metadata (suggested name, suggested category/phase, suggested location in a virtual folder tree) — the raw files stay exactly where and as they are.
- Every automated judgment (rename suggestion, phase assignment, staleness label, gap flag) should be **versioned and reversible** — full history of what the system inferred and when, so the user can inspect, override, or roll back any individual decision without losing the underlying data.

### 3.7 Self-Updating Report (Step 12) & Dashboard (Step 13)

- The report should be regenerated incrementally as new artifacts are ingested (see 3.8), not fully rewritten from scratch each time, and should read as a living project summary: current direction, recent activity, open gaps, flagged stale artifacts.
- Dashboard surfaces: full artifact browser (organized view), timeline (with confidence indicators and flagged gaps), direction/drift view (current vs. superseded artifacts with rationale), and the generated report — all pointing back to the same underlying raw files and audit trail.

### 3.8 Incremental Processing

- Design steps 5–12 to support incremental updates: when new files are added or existing ones change, only reprocess what's new/changed, then patch the timeline, embedding clusters, direction inference, and report rather than recomputing the entire project from scratch. This matters both for cost/latency and for making the "self-updating" promise feel real rather than batch-y, especially as a project's corpus grows into the hundreds of documents over months or years.

---

## 4. Non-Functional Requirements

- **Auditability:** every inferred fact (a date, an entity, a phase assignment, a staleness label, a gap) should be traceable to the source artifact(s) and signal(s) that produced it.
- **Reversibility:** no automated action should be permanent or unreviewable; the human confirmation checkpoint (3.4) and non-destructive view layer (3.6) are core to this, not optional add-ons.
- **Cost/scale target for MVP:** a single project with hundreds to low thousands of mixed-type documents, processed incrementally — not a multi-tenant, org-wide scale target yet.
- **Privacy/security:** treat ingested content as sensitive by default (research data, cost figures, collaborator names) — this affects storage/encryption choices and any use of third-party LLM APIs for the reasoning steps.
- **Extensibility:** the phase-model library (3.3) and entity-extraction schema should be structured as configuration/data so a second vertical can be added without rearchitecting the pipeline.

## 5. Explicit Non-Goals for v1

- Not building a real-time multi-user collaborative editing system.
- Not supporting arbitrary file types beyond the list in 3.1 at launch.
- Not attempting perfect automated gap detection — the goal is a strong first-pass draft a human reviews and corrects, not a fully autonomous judgment.
- Not solving cross-project analysis (comparing/relating multiple separate projects) — scope is one project's folder at a time for v1.