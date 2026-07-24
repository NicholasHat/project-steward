"""Seed the config-driven phase-template library.

Phase models are data, not code — a new domain (vertical) is added by adding an
entry here, no pipeline changes required. Idempotent: safe to re-run.

    uv run python -m truth_engine.db.seed
"""

from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from truth_engine.config import get_settings
from truth_engine.db.models import PhaseTemplate

# domain -> ordered (phase_name, description)
TEMPLATES: dict[str, list[tuple[str, str]]] = {
    "research": [
        ("Conceptualization & Planning", "Framing questions, hypotheses, study design."),
        ("Execution", "Experimentation and data collection."),
        ("Analysis", "Processing and interpreting results."),
        ("Reporting & Dissemination", "Write-ups, presentations, publication."),
    ],
    "engineering": [
        ("Initiation", "Problem framing, goals, feasibility."),
        ("Planning & Design", "Architecture, specs, plans."),
        ("Execution", "Build/implementation work."),
        ("Monitoring & Control", "Testing, review, tracking against plan."),
        ("Closure", "Release, handover, retrospective."),
    ],
    # Fallback used when domain confidence is low or nothing fits (§3.3.4).
    "generic": [
        ("Start", "Earliest activity."),
        ("Middle", "Ongoing activity."),
        ("Recent", "Most recent activity."),
    ],
}


def seed() -> int:
    engine = create_engine(get_settings().database_url)
    written = 0
    with Session(engine) as session:
        for domain, phases in TEMPLATES.items():
            for ordinal, (name, desc) in enumerate(phases):
                existing = session.scalar(
                    select(PhaseTemplate).where(
                        PhaseTemplate.domain == domain,
                        PhaseTemplate.ordinal == ordinal,
                    )
                )
                if existing is None:
                    session.add(
                        PhaseTemplate(
                            domain=domain, phase_name=name, ordinal=ordinal, description=desc
                        )
                    )
                    written += 1
                else:
                    existing.phase_name = name
                    existing.description = desc
        session.commit()
    return written


if __name__ == "__main__":
    count = seed()
    print(f"phase_templates seeded (new rows: {count})")
