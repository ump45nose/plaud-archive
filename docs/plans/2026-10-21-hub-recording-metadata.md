# Hub recording metadata — 2026-10-21

Owner approved the Hub proxy design and a targeted Plaud service restart. Existing
Hub device authentication and Range streaming provide the same audio URL on LAN and
public NPM; source bearer credentials never reach clients. Audio/transcripts stay at
Plaud and are read on demand, with no Hub file mirror or new storage/domain.

Scope: optional audio_only list filter, metadata_only detail (stat transcript path,
no text read), compatibility tests and deployment. Existing complete detail, MCP,
media paths, read/write credentials, workflow, cron IDs and pause state remain.
Aggregate main has unrelated boot edits; worktree codex/hub-v2-plaud-20261021 is used.
No merge into dirty main; publish subtree and deploy an isolated source release with
the existing runtime/venv and identity, preserving live data and OAuth.

Verification: 23 source tests pass, including no Path.read_text call during metadata
queries, empty-day filtering, missing transcript and HTTP 206 byte-range seeking.
Deployment and real Hub LAN/public read checks pending.
