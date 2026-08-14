"""Stage 9: human-approved remediation.

    The LLM proposes. Deterministic rules decide. A human approves.
    Only then may remediation execute.

Nothing in this package computes severity, reads a hypothesis for a decision, or
asks a model anything. It turns an approval a person made into an auditable,
executed-once request for human work.
"""
