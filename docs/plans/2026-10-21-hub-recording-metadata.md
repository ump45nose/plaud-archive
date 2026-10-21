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
Deployment and real Hub LAN/public read checks complete. Source code release45d4e9a,
aggregate checkpoint a25bd5d; standalone /home/yuwk/src/plaud-archive selected via one
PYTHONPATH entry in existing private server.env. Existing unit/workdir/venv and all
other env bytes preserved. Only plaud-archive.service was restarted on the source.
Hub exposes authenticated public/LAN Range proxy; MP3 and Ogg Opus returned206 for
initial and nonzero byte ranges, anonymous401, device revocation401. Metadata list
pages14+1 days over the existing15days/33audio inventory; no new source business data.
Native client decoding was not exercised. The aggregate main remains unmerged because
of unrelated boot edits; all source changes are committed in this worktree and the
independent repository main. Private rollback: /vol2/1000/Docker/hub/deployment-backups/
v2-20261021; restore only source selection/code, never overwrite current audio/DB/OAuth.
