<p align="center">
  <img src="docs/assets/logo.svg" width="128" height="128" alt="Degreaser logo: a white spray bottle with a check-mark label, spraying, on an indigo tile">
</p>

<h1 align="center">Degreaser</h1>

<p align="center"><b>Work through a messy file tree one file at a time — keep, delete, categorise — then commit.</b></p>

Degreaser is a cross-platform desktop app (macOS, Windows and Linux) for working through large, messy file trees one file at a time. Old computer backups on a Samba share are the motivating case. For each file you decide whether to **keep**, **delete**, **categorise** (tag) or **skip** it, using the keyboard and with a preview on screen. Nothing on disk changes while you triage. When you are ready, you **commit**, and Degreaser carries out the deletions and records your categories in `.degreaser/catalog.json`. The commit is resumable. Project state can be synced through the project folder, so you can continue on another computer.

> **Status:** milestone 1 is complete: the full triage-and-commit flow works on local folders (and on shares mounted by the OS). SMB access, background downloads and multi-device sync come in milestone 2; see [docs/roadmap.md](docs/roadmap.md). The documents below are the agreed specification. Read them before changing behaviour, and update them when behaviour changes.

## Running it

```sh
uv sync --all-groups
uv run degreaser                 # follows the OS light/dark setting
uv run degreaser --theme dark    # or force light / dark
```

## Documentation

| Doc | What it covers |
|---|---|
| [docs/product-spec.md](docs/product-spec.md) | What the app does, screen by screen and action by action. The source of truth for behaviour. |
| [docs/architecture.md](docs/architecture.md) | Tech stack, module layout, the `FileSource` abstraction, threading, packaging. |
| [docs/data-model.md](docs/data-model.md) | On-disk layout, SQLite schema, settings files. |
| [docs/triage-engine.md](docs/triage-engine.md) | Traversal order, decision precedence, rules, categories and autocomplete, undo, the deferred-skip queue. |
| [docs/previews.md](docs/previews.md) | File-type detection, preview renderers, lazy loading, the temp-file lifecycle. |
| [docs/scanning-and-commit.md](docs/scanning-and-commit.md) | Indexing, the startup rescan, new/changed file detection, the commit journal and resume. |
| [docs/decisions.md](docs/decisions.md) | Decision log: what we chose, what we rejected, and why. |
| [docs/roadmap.md](docs/roadmap.md) | Milestones, technical spikes and open questions. |

## Tech stack (summary)

- Python 3.14 and PySide6 6.12 (Qt 6) for the UI. Qt Multimedia's FFmpeg backend handles audio and video.
- SMB2/3 through `smbprotocol`, behind a pluggable `FileSource` interface (Local and SMB now, WebDAV later).
- One SQLite database per project.
- PyInstaller builds for each OS, distributed outside the app stores.

See [docs/architecture.md](docs/architecture.md) for details.

## License

MIT. See [LICENSE](LICENSE).

Bundled third-party assets:
- [Inter](https://rsms.me/inter/) font, by Rasmus Andersson, under the SIL Open Font License; see `src/degreaser/ui/assets/fonts/OFL-Inter.txt`.
- [JetBrains Mono](https://www.jetbrains.com/lp/mono/) font, under the SIL Open Font License; see `src/degreaser/ui/assets/fonts/OFL-JetBrainsMono.txt`.
- [Lucide](https://lucide.dev) icons, under the ISC licence; see `src/degreaser/ui/assets/icons/LICENSE-lucide.txt`.
