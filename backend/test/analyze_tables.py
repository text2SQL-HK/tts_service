import csv
import sys
from pathlib import Path
from db_utils import get_connection

current_dir = Path(__file__).resolve().parent

def analyze_and_update():
    conn = get_connection()
    c = conn.cursor()

    # 1. Add columns if not exists
    try:
        c.execute("ALTER TABLE test_cases ADD COLUMN table_names TEXT")
    except Exception as e:
        pass # Already exists

    try:
        c.execute("ALTER TABLE test_cases ADD COLUMN table_count INTEGER")
        print("Added 'table_count' column to test_cases table.")
    except Exception as e:
        pass # Already exists

    # 2. Read CSV and Update
    base_file = current_dir / "sql_with_result_new.csv"
    if not base_file.exists():
        print(f"File not found: {base_file}")
        return

    print(f"Reading {base_file}...")
    
    print("\n" + "="*80)
    print(f"{'Row':<5} | {'Count':<5} | {'Tables'}")
    print("-" * 80)
    
    with open(base_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            test_case_id = i + 1
            table_str = row.get('Table_name', '')
            
            # Simple parsing
            tables = []
            if table_str:
                cleaned = table_str.replace('[', '').replace(']', '').replace("'", "")
                tables = [t.strip() for t in cleaned.split(',') if t.strip()]
            
            table_count = len(tables)
            
            # Update DB
            c.execute("UPDATE test_cases SET table_names = ?, table_count = ? WHERE id = ?", (table_str, table_count, test_case_id))
            
            # Print info
            display_tables = ", ".join(tables)
            if len(display_tables) > 60:
                display_tables = display_tables[:57] + "..."
                
            print(f"{test_case_id:<5} | {table_count:<5} | {display_tables}")

    conn.commit()
    conn.close()
    print("="*80)
    print("Analysis and Update Complete.")

if __name__ == "__main__":
    analyze_and_update()
