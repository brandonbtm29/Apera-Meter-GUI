import os
import csv
import glob

# Try to find backup files
home = os.path.expanduser("~")
backup_dir = os.path.join(home, "Documents", "Conductivity_App_Backups")
files = glob.glob(os.path.join(backup_dir, "*.csv"))
if files:
    for file in files:
        print(f"\n--- {os.path.basename(file)} ---")
        with open(file, encoding="utf-8") as f:
            reader = csv.reader(f)
            header = next(reader, None)
            print(header)
            rows = list(reader)
            print(f"Total rows: {len(rows)}")
            for row in rows[:10]:
                print(row)
