"""Step 4 — Store.

Repository/persistence helpers over db.models: durable normalized rows plus
text blobs, written transactionally per artifact. Every write that records an
inferred fact also appends to decision_audit (the reversibility backbone).

Failure modes: partial writes (transactional per artifact), large-blob handling.

Pipeline logic is intentionally not implemented yet — this is scaffolding.
"""
