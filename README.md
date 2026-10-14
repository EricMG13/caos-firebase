# CAOS on Firebase

Governed source documents to committee-ready credit conclusions, deployed on Firebase
and Google Cloud. A fork of the enterprise `caos-databricks` build with the Databricks
platform unplugged: Cloud Run serves the workspace, API and run stream; Cloud SQL holds
the store; a Cloud Storage bucket holds the documents; Firebase Auth signs people in;
OpenRouter answers model calls.

- Design: [`docs/specs/2026-10-04-firebase-design.md`](docs/specs/2026-10-04-firebase-design.md)
- Decisions: [`docs/decisions.md`](docs/decisions.md)
- Agent contract: [`CLAUDE.md`](CLAUDE.md)

Status: re-forked onto `caos-databricks` (FD15); Phase 1 and hardening H1-H5 landed
2026-10-11. Deploy follows [`docs/DEPLOY.md`](docs/DEPLOY.md).
