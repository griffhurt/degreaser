# Degreaser user guide

Degreaser helps you clean up a big pile of files, such as old computer backups, one file at a
time. You see each file with a preview, decide what to do with a single key, and nothing
happens to your files until you review your decisions and **commit** them.

- [Installing](#installing)
- [Your first project](#your-first-project)
- [Triage](#triage)
- [Categories](#categories)
- [Duplicates](#duplicates)
- [Review and commit](#review-and-commit)
- [Using more than one computer](#using-more-than-one-computer)
- [Preferences](#preferences)
- [Where things are kept](#where-things-are-kept)
- [Troubleshooting](#troubleshooting)

## Installing

Download the latest release from the [releases page](https://github.com/griffhurt/degreaser/releases).

**macOS** (14 Sonoma or later, Apple silicon): open `Degreaser-…-macOS.dmg` and drag
Degreaser into Applications.

**Windows** (10 or 11, 64-bit): run `Degreaser-…-Windows-Setup.exe`. It installs for your
user account only, so it doesn't need an administrator. The installer isn't code-signed yet,
so Windows may say "Windows protected your PC": click **More info**, then **Run anyway**.

**Linux** (64-bit): download `Degreaser-…-x86_64.AppImage`, make it executable
(`chmod +x Degreaser-*.AppImage`, or tick "Allow executing" in its properties) and run it.

**Updates:** Degreaser checks for updates once a day. On macOS and Windows it downloads and
installs them for you after asking; on Linux it tells you a new version is out and opens the
download page. You can turn automatic checks off, or check right away, in
[Preferences](#preferences) → General.

## Your first project

A **project** is one folder you want to clean up. Click **New Project** and fill in:

- **Name:** anything that helps you recognise it.
- **Source:**
  - **Local folder** for a folder on this computer, an external drive, or a network share
    your computer has already mounted.
  - **SMB share** to connect to a NAS or file server directly. You can paste an address like
    `smb://nas/backups/Laptops` or `\\nas\backups\Laptops` into the server field and the rest
    fills itself in. **Test Connection** checks the details before you save. Your password
    is kept in your system's password store (Keychain, Windows Credential Manager or
    Secret Service), never in the project.
- **Settings:** the defaults suit most people. You can change them later from the project
  list (**Edit…**) or inside a project (Project → Project Settings…).
  - *Re-triage files that changed since they were triaged:* if a file is modified after you
    decided on it, you'll be asked about it again.
  - *Remove folders left empty by commit.*
  - *Jump back to skipped files once their preview has loaded:* see [Skipping](#skipping).
  - *Sync project state to the project folder:* see [Using more than one computer](#using-more-than-one-computer).
  - *When committing deletions:* move files to a Degreaser trash folder (the default, and
    recoverable) or delete them permanently.
  - *Duplicates:* see [Duplicates](#duplicates).
- **Rules:** files that always get the same decision, so you never see them. *Always delete*
  presets cover things like system files (`.DS_Store`, `Thumbs.db`), caches and build output;
  *always keep* presets cover photos, documents and so on. Hover over a preset to see exactly
  what it matches. **Edit as Text** lets you write your own patterns (one per line, in the
  same style as a `.gitignore` file).

Save, and the project opens. The first time, Degreaser starts showing files straight away
while it lists the rest of the folder in the background ("Indexing… (1,204 of 9,330
folders)" in the top bar). On a big network share this can take a while; you can work the
whole time, and it picks up where it left off next time.

## Triage

The triage window shows one file at a time:

- **Path bar** (top): where the file is. Click any folder in it to see what's inside, jump to
  another file, or keep, delete or tag that whole folder.
- **Banner:** appears when the file needs extra attention (see [below](#banners)).
- **Preview** (left) and **details** (right): the file's contents and information about it,
  including its category and any duplicates.
- **Action bar** (bottom): the actions, each with its key.

### Keys

| Key | Action |
|---|---|
| `K` | **Keep** the file |
| `D` | **Delete** the file |
| `T` | **Tag:** give the file a category, which also keeps it |
| `S` | **Skip:** decide later |
| `⇧K` | **Keep folder:** keep everything in this file's folder, including subfolders |
| `⇧D` | **Delete folder:** delete this file's folder and everything in it |
| `⇧T` | **Tag folder:** give the whole folder a category |
| `O` | **Open** the file in its usual app |
| `Space` | **Expand** the preview (load more text, play a video, and so on) |
| `⌘Z` / `Ctrl+Z` | **Undo**, including folder actions |
| `⇧⌘Z` / `Ctrl+Shift+Z` | **Redo** |

Every key can be changed in [Preferences](#preferences) → Shortcuts.

After each decision Degreaser moves on to the next file that needs one. Folder actions skip
the rest of that folder. If a folder you're about to delete contains files you already chose
to keep, or a folder you're tagging contains files you marked for deletion, you're asked
first.

Your decisions are saved the moment you make them. You can quit at any time, even mid-folder,
and carry on where you left off, and undo still works after a restart.

### Previews

Degreaser previews text and code (with syntax colouring), images (including AVIF, and HEIC on
macOS), video and audio, PDFs, Word/Excel/PowerPoint and OpenDocument
files, Pages/Numbers/Keynote (their preview image), and the contents of zip, 7z, RAR and tar
archives. Anything else shows its first bytes and an **Open** button.

To keep things quick, especially over a network, previews read only what they need: the
start of a text file, an archive's table of contents. Press **Space** for more. Files over
250 MB that have to be read in full show a **Load it** button (or press Space) instead of
loading by themselves.

### Skipping

**Skip** (`S`) puts a file aside. When you reach the end, **Review Skipped** goes through the
skipped files again.

On a network share, if you skip a file whose preview is still loading, Degreaser keeps
loading it in the background and brings it back as soon as it's ready, with a *Skipped
earlier — now loaded* banner. This means a slow file never holds you up. (Turn this off with
the *jump back* setting.)

### Banners

| Banner | Meaning |
|---|---|
| **Newly added file** | The file appeared since your last session. |
| **Changed since you triaged it** | The file was modified after you decided on it. Your earlier decision is shown. |
| **Skipped earlier — now loaded** | A file you skipped while it was loading is ready. |
| **Previously skipped** | You're reviewing skipped files. |
| **Duplicate** | Other files in the project are identical to this one. |

## Categories

A **category** says what a file is, from broad to specific, like
`Videos › High School › Senior Film Project`. Categories are recorded in a catalog when you
commit, and a future version will be able to reorganise your kept files into folders named
after them.

Press `T` (or `⇧T` for the folder) and type the category's parts separated by commas:
`Videos, High School, Senior Film Project`.

- As you type, Degreaser suggests a completion in grey. **Tab** accepts it and moves on to the
  next part; press **Tab** again to see the next suggestion, **Shift+Tab** to go back.
- Suggestions favour what usually comes next: after `Videos,` you'll see the parts you've used
  after `Videos` before.
- **Enter** saves the category exactly as typed. **Esc** cancels.
- Files inherit their folder's category, and the field starts with it filled in, so in a
  folder tagged `Videos, High School` you can press `T`, type `, Senior Film Project` and
  press Enter.
- Because each part may become a folder name, some characters (`/ \ : * ? " < > |`) aren't
  allowed; they're underlined as you type.

## Duplicates

Once the background indexing finishes, Degreaser looks for files that are byte-for-byte
identical ("Finding duplicates…" in the top bar; you can pause it from the Project menu).
It only reads files that could be duplicates, starting with a quick check of their first and
last bytes, so it's gentle on network shares.

When a file has copies, a **Duplicate** banner says where they are and what you decided for
them, and the details panel lists every copy.

Two settings in the project's settings control this:

- **Ignore files smaller than** (1 MB by default): tiny files make lots of uninteresting
  matches.
- **Delete exact duplicates of files I've kept** (off by default): once you've kept one copy,
  the others are marked for deletion automatically. It never overrides a decision you made
  yourself, or a folder you chose to keep. Before deleting, the commit checks the copy you
  kept is still there and unchanged.

The review screen's **Duplicates** filter shows copies grouped together, with **Keep One,
Delete the Others…**.

## Review and commit

When you've worked through everything (or whenever you like), open
Project → **Review & Commit…**. The review screen lists every decision. Filter by delete,
keep, category, skipped, duplicates, files a rule decided, and more. You can change decisions
there, for one file or many at once. Hover over a row to see its full path, and double-click
a file to see it in the triage window.

**Commit…** shows exactly what will happen ("move 12,408 files — 41.7 GB — to the Degreaser
trash…") and asks you to confirm. Then it:

1. Moves each file and folder marked for deletion to the **Degreaser trash** (or deletes it,
   if you chose that). Before touching a file it checks the file hasn't changed since
   Degreaser last saw it; changed files are left alone and listed in the report.
2. Writes the **catalog** of categories (`.degreaser/catalog.json` in the project folder).
3. Removes folders the commit left empty, if that setting is on.

You can pause or cancel a commit. If the connection drops it waits and carries on, and if
Degreaser quits halfway, it offers to resume the next time you open the project. Skipped
and undecided files are never touched.

The **Degreaser trash** is a folder inside the project (`.degreaser/trash/`), arranged like
the original folders, so you can move anything back by hand. On a network share, moving there
is instant and uses no extra space. When you're sure, Project → **Empty Degreaser Trash…**
deletes it for good.

## Using more than one computer

With *Sync project state to the project folder* on, Degreaser saves your decisions,
categories, rules and settings inside the project folder (`.degreaser/state/`), so another
computer that can reach the same folder can carry on where you left off. Only one computer
can make decisions at a time.

- **On the second computer**, click **Add Existing…** on the project list and choose the same
  folder. Its name, settings and decisions come from the folder.
- **If another computer has the project open**, you'll see which one and when it last checked
  in. You can **Open Read-Only** (look, but not change anything), **Take Over** (that
  computer's unsaved work since its last sync is lost, and its window becomes read-only), or
  cancel.
- If both computers changed the project since they last synced, you choose whose changes to
  keep.

Passwords and your Preferences (shortcuts, theme and so on) stay on each computer.

## Preferences

Open Preferences with the gear button on the project list, or `⌘,` / `Ctrl+,` anywhere
(on macOS also Degreaser → Settings…).

- **General:** light or dark theme (or follow the system); this computer's name, which other
  computers see when it has a synced project open; automatic update checks and **Check Now**.
- **New Projects:** the settings new projects start with.
- **Shortcuts:** click a shortcut, then press the new key combination. Two actions can't
  share a key, and Degreaser won't let you save until each action has its own.
- **Previews:** how large a file may be before previews ask first, how much of a text file
  to read, when to warn before a large download, how many downloads to run at once, and
  how big the download cache may grow. Also: play videos automatically (muted).

## Where things are kept

- **Your files** are only changed by a commit (and by emptying the Degreaser trash).
- **In the project folder**, Degreaser keeps a hidden `.degreaser` folder: the catalog, the
  Degreaser trash and, with sync on, the synced state.
- **On this computer**, each project's list of files and decisions is kept in Degreaser's
  app data folder: `~/Library/Application Support/Degreaser` on macOS,
  `%LOCALAPPDATA%\Degreaser` on Windows and `~/.local/share/Degreaser` on Linux. A project
  with a million files takes about a gigabyte.
- **Removing a project** from the list deletes only that local data, never your files or the
  `.degreaser` folder.

## Troubleshooting

**The SMB connection fails.** Check the server name, share and folder with **Test
Connection**. Most errors say what's wrong (wrong password, share not found, server not
reachable). If the server only offers old SMB versions, leave *Always encrypt* off.

**A project opens read-only.** Another computer has it open, or you chose Open Read-Only. A
window also becomes read-only if another computer takes the project over.

**The triaged count jumps while indexing.** Totals count the files found so far, so they
grow as indexing lists more folders, and they're recounted every half a minute while it
runs.

**A video shows "No picture could be decoded".** Your computer has no decoder for that
video's format. AV1 video on Windows needs hardware support, for example. **Open** plays it
in another app if one can.

**HEIC photos don't preview on Windows or Linux.** For now they preview only on macOS; use
**Open** elsewhere.

**Something else is wrong.** Please [open an issue](https://github.com/griffhurt/degreaser/issues)
with what you did and what happened.
