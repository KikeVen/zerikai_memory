import argparse
import sqlite3
import sys
from pathlib import Path

try:
    from config import ZERIKAI_DB
    from main import DB_PATH, _db_lock, db_client
except ImportError as e:
    print(f"ERROR: Could not import required modules: {e}")
    sys.exit(1)

def resolve_workspace_identifier(identifier):
    """
    Resolves a user-provided identifier to a workspace UUID.
    Accepts either a display name or UUID.
    
    Returns:
        (workspace_uuid, display_name) if found, else (None, None)
    """
    try:
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        # Try as UUID first
        cursor.execute(
            "SELECT workspace_uuid, display_name FROM workspace_registry WHERE workspace_uuid = ?",
            (identifier,)
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return (row["workspace_uuid"], row["display_name"])
        
        # Try as display name
        cursor.execute(
            "SELECT workspace_uuid, display_name FROM workspace_registry WHERE display_name = ?",
            (identifier,)
        )
        row = cursor.fetchone()
        if row:
            conn.close()
            return (row["workspace_uuid"], row["display_name"])
        
        conn.close()
        return (None, None)
        
    except Exception as e:
        print(f"ERROR: Failed to query workspace registry: {e}")
        return (None, None)

def drop_workspace_memory(identifier):
    """
    Deletes all memory data for a workspace: vectors, context file, and registry entry.
    Accepts either a display name or UUID.
    """
    workspace_uuid, display_name = resolve_workspace_identifier(identifier)
    
    if not workspace_uuid:
        print(f"ERROR: Workspace '{identifier}' not found in registry.")
        print("Run 'python main.py list_workspaces' to see available workspaces.")
        return
    
    print(f"--- Wiping memory for workspace: {display_name} ({workspace_uuid[:8]}) ---")
    
    # 1. Delete the ChromaDB collection (vectors)
    collection_name = f"memory_{workspace_uuid}"
    with _db_lock:
        try:
            db_client.delete_collection(collection_name)
            print(f"SUCCESS: Vector collection '{collection_name}' has been completely deleted.")
        except ValueError:
            # ChromaDB raises ValueError if the collection does not exist
            print(f"SKIPPED: Vector collection '{collection_name}' does not exist.")
        except Exception as e:
            print(f"ERROR: Failed to delete collection '{collection_name}': {e}")
            
    # 2. Delete the context Markdown file
    context_file = Path(DB_PATH) / "contexts" / f"{workspace_uuid}.md"
    if context_file.exists():
        try:
            context_file.unlink()
            print(f"SUCCESS: Context file '{context_file.name}' has been deleted.")
        except Exception as e:
            print(f"ERROR: Failed to delete context file '{context_file.name}': {e}")
    else:
        print(f"SKIPPED: Context file '{context_file.name}' does not exist.")
    
    # 3. Delete the workspace registry entry
    try:
        conn = sqlite3.connect(str(ZERIKAI_DB), timeout=10)
        conn.execute("DELETE FROM workspace_registry WHERE workspace_uuid = ?", (workspace_uuid,))
        conn.commit()
        conn.close()
        print("SUCCESS: Workspace registry entry deleted.")
    except Exception as e:
        print(f"ERROR: Failed to delete workspace registry entry: {e}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Completely wipe a workspace's vectors, context file, and registry entry."
    )
    parser.add_argument(
        "identifier",
        help="The workspace display name or UUID (e.g., 'zerikai_memory' or 'a3f8c2d1-...')"
    )
    args = parser.parse_args()
    
    drop_workspace_memory(args.identifier)
