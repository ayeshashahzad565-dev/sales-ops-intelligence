"""Stage 7: LLM root-cause hypothesis generation.

The first part of this project permitted to call a language model, and the last
one that would be missed if it were switched off. Stages 5 and 6 decide what is
unusual and what matters; this package explains what might have caused it.

    models.py    the evidence package in, the validated hypothesis out
    prompts.py   how evidence becomes a prompt, and the prompt version
    provider.py  the provider interface, one real implementation, one fake
    service.py   eligibility, orchestration, per-anomaly failure isolation

Nothing here talks to PostgreSQL. Evidence arrives as a value object and the
finished hypothesis leaves as one; the SQL lives in `analytics/repository.py`
with every other query in the service. That keeps the prompt and the validation
testable without a database, and keeps the database reviewable without reading
prompt text.

The boundary this package exists to hold
----------------------------------------
The model receives Stage 6's verdict as CONTEXT and is asked to explain it. It
is never asked whether the verdict is right. Its response schema has no field
for severity, routing or decision; the validator rejects unknown fields; and the
database re-checks the snapshot with a trigger before accepting a row. Three
independent layers, because the interesting failure is not the model getting a
cause wrong - it is the model being quietly promoted to decision-maker.
"""
