# Safe Upgrade Process

Upgrading zerikai_memory while keeping your already indexed workspaces is safe **as long as the workspace path never changes**.

This guide explains why, and gives you the exact steps to upgrade without losing your indexed memory, project briefs, or workspace identity.

---

## Why the path matters

Every workspace is identified by a **stable UUID derived deterministically from its absolute filesystem path** (`_derive_workspace_id` in `main.py`). The path is normalized (case, separators, trailing slashes) and then looked up in the SQLite workspace registry (`.brain/zerikai.db`). Same path → same UUID, forever.

Everything hangs off that UUID:

| Artifact | Location |
|---|---|
| ChromaDB collection | `memory_{workspace_id}` |
| Project brief | `.brain/contexts/<workspace_id>.md` |
| Registry row | `.brain/zerikai.db` keyed by normalized path |

Because `.brain/` is gitignored (`.gitignore`), a `git pull` **never touches it**. Your UUID, collections, and briefs survive an upgrade untouched.

---

## The safe way: upgrade in place

Do **not** copy, rename, clone, or move the project directory. If the new directory lands at a different path, `_derive_workspace_id` computes a **new UUID**, the registry lookup misses, and your old collection and brief become **orphaned** — invisible to the new workspace.

### Step 1 — Stop the MCP server

Close the IDE(s) running the server, or kill the `main.py` process. This releases the ChromaDB file lock on `.brain/vector_db/` and prevents corruption.

### Step 2 — Back up `.brain/`

Back up your **already-indexed data** so you can restore it later and never have to re-index. This preserves your ChromaDB vectors, project briefs, and workspace registry. Do **not** rename the whole project — only back up the data directory (`.brain/`).

```bash
# Windows (PowerShell)
Copy-Item -Recurse .brain .brain.bak

# macOS / Linux
cp -r .brain .brain.bak
```

After you upgrade and restart, this backup lets you restore your memory exactly as it was — no re-indexing needed.

### Step 3 — Pull the latest code

```bash
git pull
```

If you have local changes you want to discard, use:

```bash
git fetch origin
git reset --hard origin/main
```

### Step 4 — Reinstall dependencies (only if changed)

If `requirements.txt` changed in the update:

```bash
# Windows
.\venv\Scripts\python.exe -m pip install -r requirements.txt

# macOS / Linux
venv/bin/python -m pip install -r requirements.txt
```

### Step 5 — Restart and verify

Restart the MCP server in your IDE, then confirm your workspace still resolves to the same UUID:

```
"List my workspaces."
```

Your existing workspace should appear with its original UUID and brief intact. If you want to be certain, run `debug_workspace_id` on the project path — it must return the same UUID as before the upgrade.

---

## Restoring from a backup

The whole point of the backup is to **restore your already-indexed data**. After you upgrade, restore the backed-up `.brain/` directory so your memory comes back exactly as it was — ChromaDB vectors, project briefs, and workspace registry intact. No re-indexing needed:

```bash
# Windows (PowerShell)
Remove-Item -Recurse .brain
Copy-Item -Recurse .brain.bak .brain

# macOS / Linux
rm -rf .brain
cp -r .brain.bak .brain
```

Then restart the server. Your UUID, collections, and briefs are back exactly as they were.

---

## What NOT to do

| ❌ Don't | Why |
|---|---|
| Rename the folder (e.g. `zerikai_memory_old`) then clone fresh | New path → new UUID → old collection/brief orphaned |
| Clone into a differently-named folder | Same problem — new UUID, empty workspace |
| Move the project to a different parent directory | Same problem — new UUID, empty workspace |
| Open through a symlink/junction that resolves differently | Normalization may produce a different path → new UUID |
| Keep two live copies of the same `.brain/` | Two registry rows, duplicate workspaces in `list_workspaces` |

---

## Recovery: I already broke it

If you already moved the project and ended up with an empty workspace, your old data is **not lost** — it is orphaned under the old UUID. Recover it with `merge_workspaces`:

```
"Merge workspaces <old-uuid> into <new-uuid>"
```

`merge_workspaces` moves the ChromaDB collection from the source workspace into the target, then deletes the source. **Irreversible** — the source is deleted after the merge. Run `list_workspaces` first to identify both UUIDs.

---

## Summary

- **Upgrade in place** with `git pull` — never copy/rename/clone/move.
- **Back up `.brain/`** before upgrading.
- **Stop the server** first to avoid ChromaDB file-lock corruption.
- **Same path = same UUID** = your memory survives the upgrade untouched.
