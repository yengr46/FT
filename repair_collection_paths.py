"""
repair_collection_paths.py
Run this ONCE with the FT app closed to fix the tab-corrupted collection paths.
Caused by a bug in migrate_txt_to_db that used "\\t" instead of "\t" when
parsing old _tags_*.txt files — the tab+timestamp was stored as part of the path.
"""
import sqlite3, os, shutil, glob

# Find the DB
script_dir = os.path.dirname(os.path.abspath(__file__))
pattern    = os.path.join(script_dir, "main", "FTProj_*", "FileTagger.db")
dbs        = glob.glob(pattern)
if not dbs:
    pattern = os.path.join(script_dir, "**", "FileTagger.db")
    dbs     = glob.glob(pattern, recursive=True)

if not dbs:
    print("No FileTagger.db found. Make sure this script is in the FTAPPS folder.")
    input("Press Enter to exit.")
    raise SystemExit(1)

for db_path in dbs:
    print(f"\nRepairing: {db_path}")
    # Backup
    bak = db_path + ".bak_tab_repair"
    shutil.copy2(db_path, bak)
    print(f"  Backup:  {bak}")

    db = sqlite3.connect(db_path)
    row = db.execute(
        'SELECT COUNT(*) FROM collection_items WHERE path LIKE "%" || char(9) || "%"'
    ).fetchone()
    n = row[0]
    print(f"  Corrupted paths: {n}")
    if n:
        db.execute('''
            UPDATE collection_items
            SET path = SUBSTR(path, 1, INSTR(path, char(9)) - 1)
            WHERE path LIKE "%" || char(9) || "%"
        ''')
        db.commit()
        print(f"  Fixed {n} paths.")
    else:
        print("  Nothing to fix.")
    db.close()

print("\nDone. You can delete the .bak_tab_repair files once you've verified collections look correct.")
input("Press Enter to exit.")
