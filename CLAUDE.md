# Degreaser — guidance for Claude

Read [README.md](README.md) and the docs in `docs/` before changing behaviour. `docs/product-spec.md` is the source of truth for behaviour, and `docs/decisions.md` records why things are the way they are. When behaviour changes, update the relevant doc in the same change. When a decision changes, add a new entry to `docs/decisions.md` instead of rewriting the old one.

## Invariants

- **Triage never modifies user files.** The only writes to a file source outside a commit are under `<project root>/.degreaser/state/` (sync and lock).
- `core/` has **no Qt imports**. All file I/O goes through `core/sources` (`FileSource`). The UI thread never does source I/O.
- Paths are relative POSIX strings (root = `""`), stored exactly as the source returns them. Don't normalise them for identity.
- Effective state (action and category) is computed by the resolver and never stored.
- Decision-state tables key on `path`, not `nodes.id`, because they are synced between devices.
- Every commit op must be idempotent and must re-verify size/mtime before deleting.
- The app is MIT-licensed. Don't add GPL or AGPL dependencies. LGPL and MPL are fine when shipped as separate libraries.

## Commands

Not set up yet; this section gets filled in during M0. The plan is `uv run pytest`, `uv run ruff check`, `uv run pyright`, and `uv run degreaser`.
