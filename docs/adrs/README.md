# Architecture decision records

ADRs record significant decisions and their rationale, next to the code. Keep each record to one decision and one or two pages. Routine fixes and refactors that preserve interfaces and ownership do not need one.

Use `YYYYMMDD-kebab-title.md`, with the creation date matching the Date field. Use the [template](template.md), open a PR with status `proposed`, and discuss the context, options and consequences. These conventions follow [NVKE's ADR guidance](https://github.com/dsx-ai-factory/nvke/blob/main/docs/adrs/20261014-adr-conventions.md).

- `proposed`: available for discussion, including after merge; not binding.
- `accepted`: explicitly accepted through review.
- `rejected`: retained with the decision.
- `superseded by <relative path>`: replaced by a later record; retain the original.

Do not rename a record when its status changes. After acceptance, supersede it with a new record rather than rewriting the decision. A detailed design may live in `docs/design/` when needed and be linked as context; an ADR records the architectural choice rather than an implementation or rollout checklist. Reference material must be accessible to contributors. Internal experiment logs are not ADRs.

## Records

- [Provider ownership and lifecycle boundaries](20261119-provider-boundaries.md) — proposed.
