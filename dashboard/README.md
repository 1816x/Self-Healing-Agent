# dashboard

Next.js incident dashboard — reads `incidents.db` and shows each incident's
pipeline state (detected → diagnosing → diagnosed → fix proposed → PR opened)
with a tool-call trace and diff view.

Scaffolding lands in Phase 5, once there's incident data worth displaying.
Bootstrapping it earlier would mean a lockfile and a build target with
nothing behind them — see `PLAN.md` for the full phase plan.
