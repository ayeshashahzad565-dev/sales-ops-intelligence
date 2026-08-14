# Limitations

Stated plainly, because a portfolio project that claims otherwise is a worse
portfolio project.

[← back to the README](../README.md)

---

**Security**

- **Actors are asserted, not authenticated.** Every `actor` in this system —
  `dana@finance`, `priya@revops`, `stage10-recovery` — is a string the caller
  supplied. Nothing proves the caller is who it says. The audit trail records
  what was *claimed*, faithfully and immutably, and that is all it can do. This
  is the platform's largest security limitation. It is stated on the Audit Trail
  dashboard as well as here.
- **No API authentication.** The analytics service has no auth layer. Anything
  that can reach port 8001 can approve a review and execute an action.
- **No dashboard authentication beyond the Metabase login,** and no per-user
  permissions inside it. The read-only role bounds what a viewer can *do*; it
  does not bound what they can see.
- **Ports are published to localhost** for development convenience.
- **This is not production-secure.** It is a local development stack, and it has
  never been anything else.

**Scope**

- **Remediation actions have no external side effect.** The provider records
  what it was asked to do; there is no ERP, CRM or payment gateway behind it,
  and the code says so rather than pretending. Building a fake one would have
  made the audit trail a work of fiction.
- **The order data is synthetic**, generated from a fixed seed, with anomalies
  injected on purpose.
- **One LLM provider, one prompt version.** No fallback model, no ensemble, no
  evaluation harness for hypothesis quality — the system is built so that a bad
  hypothesis is inert, not so that hypotheses are good.
- **`load_staged_batch()` duplicates the Stage 3 workflow's validation rules,**
  because that logic lives in n8n node parameters where PostgreSQL cannot reach
  it. Both implementations are tested independently against the same documented
  rules.
- **Nothing escalates.** Overdue reviews and degraded pipelines are labelled and
  reported; nobody is paged.
- **The dead-letter trail grows without bound.** Retention never deletes a
  `failed` staging row — stricter than it needs to be, and the only default that
  cannot lose evidence.
- **Single-node everything.** One Postgres, one n8n, no queue, no replication.
