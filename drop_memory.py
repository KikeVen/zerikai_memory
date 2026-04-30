import sys
import argparse
from pathlib import Path

try:
    from main import db_client, _db_lock, DB_PATH
except ImportError as e:
    print(f"ERROR: Could not import from main.py: {e}")
    sys.exit(1)

def drop_workspace_memory(workspace_id):
    print(f"--- Wiping memory for workspace: {workspace_id} ---")
    
    # 1. Delete the ChromaDB collection (vectors)
    collection_name = f"memory_{workspace_id}"
    with _db_lock:
        try:
            db_client.delete_collection(collection_name)
            print(f"SUCCESS: Vector collection '{collection_name}' has been completely deleted.")
        except ValueError as e:
            # ChromaDB raises ValueError if the collection does not exist
            print(f"SKIPPED: Vector collection '{collection_name}' does not exist.")
        except Exception as e:
            print(f"ERROR: Failed to delete collection '{collection_name}': {e}")
            
    # 2. Delete the context Markdown file
    context_file = Path(DB_PATH) / "contexts" / f"{workspace_id}.md"
    if context_file.exists():
        try:
            context_file.unlink()
            print(f"SUCCESS: Context file '{context_file.name}' has been deleted.")
        except Exception as e:
            print(f"ERROR: Failed to delete context file '{context_file.name}': {e}")
    else:
        print(f"SKIPPED: Context file '{context_file.name}' does not exist.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Completely wipe a workspace's vectors and context file.")
    parser.add_argument("workspace_id", help="The workspace ID to delete (e.g., app_zerikai_deefc3)")
    args = parser.parse_args()
    
    drop_workspace_memory(args.workspace_id)
