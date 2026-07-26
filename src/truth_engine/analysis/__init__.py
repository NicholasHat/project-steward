"""Steps 6-10 — Analysis engines.

    timeline   (6)   multi-signal date resolution -> timeline_events w/ confidence
    phases     (7)   domain classification -> phase-template mapping (config-driven)
    direction  (8-9) cluster drift (Signal A) + citation graph (Signal B)
                     -> current / superseded / unclear labels, each with rationale
                     -> HUMAN CONFIRMATION CHECKPOINT before anything acts on labels
    gaps       (10)  structural (phase coverage) + promised-but-unfulfilled

LLM reasoning (via reasoning.providers) is used only for judgment calls here;
the deterministic signals (dates, embeddings, graph edges) are computed first
and are always inspectable.

timeline (6), phases (7), and direction/drift (8-9, including the citation
graph in `graph.py`) are implemented; gaps (10) is still scaffolding.
"""
