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

## UI styling

- Never hard-code colours, fonts or radii in widgets. Use `theme().tokens` and the stylesheet's `variant`/`role` properties (docs/architecture.md §7).
- Icons come from `ui/assets/icons` (Lucide). To add one, fetch it from `lucide-static` at the same version and keep the licence file.
- Check visual changes by rendering windows offscreen (`QWidget.grab().save(...)`) in both `--theme light` and `--theme dark`.

## Commands

```sh
uv sync --all-groups                      # set up .venv (Python 3.14)
uv run degreaser                          # run the app
uv run pytest                             # all tests (SMB ones skip if the container isn't up)
uv run ruff check . && uv run ruff format --check . && uv run pyright
uv run --group build pyinstaller packaging/degreaser.spec --noconfirm   # package → dist/
QT_QPA_PLATFORM=offscreen uv run degreaser --smoke-test                 # self-check (JSON)
uv run degreaser --smb-probe smb://host/share/folder --username NAME     # read-only server check (JSON)

# Samba test server (127.0.0.1:1445, user/pass degreaser/degreaser, shares: data, bench)
docker compose -f tests/integration/docker-compose.yml up -d --build --wait
tests/integration/samba/set-latency.sh 10   # add 10 ms RTT; 0 removes it
```

## Gotchas

- `smbclient` path-based calls do **not** remember a non-default port from `register_session`. Pass `port=` (and credentials) on every call.
- For SMB listings, read `DirEntry.smb_info` (size and times from the directory query itself). `DirEntry.stat()` makes an extra round trip per entry, and it drops the port.
- `import smbclient` doesn't import `smbclient.shutil`; import it explicitly.
- Ruff formats Python code blocks inside Markdown, so `*.md` is excluded to keep the docs' hand-aligned examples.
- **Never** pass a Python `QIODevice` to `QMediaPlayer.setSourceDevice()`, and never call `setSourceDevice(None)`. Both deadlock, because Qt's FFmpeg thread needs the GIL while the UI thread waits on it. SMB media goes through the loopback HTTP server (docs/previews.md §2).
- Open SMB files with `share_access="rwd"`. For hot SMB paths (directory listings), parse response buffers with `struct`; smbprotocol's generic parsing is slow.
- To identify threads in callbacks from Qt-owned threads, use `threading.get_native_id()`.
- PyInstaller can't see modules that are only imported dynamically. `--smoke-test` will fail if one of them is missing from the bundle, so add it to `hiddenimports` in `packaging/degreaser.spec`.
