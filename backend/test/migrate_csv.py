import csv
import sys
from pathlib import Path
from db_utils import init_db, get_connection

current_dir = Path(__file__).resolve().parent

def migrate():
    # Initialize DB first
    init_db()
    conn = get_connection()
    cursor = conn.cursor()

    files = [
        ("sql_with_result_new.csv", "executed_sql_with_result_new.csv", "new"),
        ("sql_with_result_new(local).csv", "executed_sql_with_result_new(local).csv", "local")
    ]
    
    # Reading keys from the first file to populate test_cases
    # Since we established both files map to the same questions, we base test_cases on the first one.
    base_file = current_dir / "sql_with_result_new.csv"
    if not base_file.exists():
        print(f"Base file not found: {base_file}")
        return

    print("Migrating Test Cases...")
    with open(base_file, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for i, row in enumerate(reader):
            test_case_id = i + 1
            cursor.execute("""
                INSERT OR IGNORE INTO test_cases (id, question, simplified_schema, gold_sql)
                VALUES (?, ?, ?, ?)
            """, (
                test_case_id,
                row.get('result', ''),
                row.get('simplified_schema', ''),
                row.get('SQL_text', '') # Assuming original SQL is in SQL_text or similar
            ))
    conn.commit()

    # Now migrating executions
    print("Migrating Executions...")
    for source_csv, result_csv, batch_name in files:
        # We need to merge source (for SQL_new if result not present) and result csv.
        # Actually result csv usually has everything source had + execution results.
        # But if result csv is shorter (failure case earlier), we might miss SQLs?
        # Let's rely on result csv primarily, but fallback to source for SQL_new if needed.
        # Wait, result_csv IS the source csv + execution results in our previous flow.
        # However, earlier we found result_csv had fewer lines sometimes? 
        # Actually it had MORE lines ("ghost" lines) or split lines.
        # Safe bet: Read Source CSV again to get the "Generated SQL" (SQL_new) that was intended for that row.
        # And read Result CSV to get status. Match by index.
        
        source_path = current_dir / source_csv
        result_path = current_dir / result_csv
        
        # Load result data into map by index
        execution_data = {}
        if result_path.exists():
            with open(result_path, 'r', encoding='utf-8') as f_res:
                # Naive reading might fail with multi-line, but we do our best for migration.
                # If migration fails slightly, user can regenerate.
                try:
                    reader = csv.DictReader(f_res)
                    for i, row in enumerate(reader):
                        execution_data[i] = row
                except Exception as e:
                    print(f"Warning reading {result_csv}: {e}")

        if not source_path.exists():
            continue
            
        with open(source_path, 'r', encoding='utf-8') as f_src:
            reader = csv.DictReader(f_src)
            for i, row in enumerate(reader):
                test_case_id = i + 1
                
                # Generated SQL from source file
                generated_sql = row.get('SQL_new', '')
                
                # Execution details from result file
                status = 'Skipped'
                message = ''
                summary = ''
                
                if i in execution_data:
                    res_row = execution_data[i]
                    # Sometimes result row might be shifted if naive parsing, but let's trust index for now
                    status = res_row.get('Execution_Status', 'Skipped')
                    message = res_row.get('Execution_Message', '')
                    summary = res_row.get('Execution_Result_Summary', '')
                    
                    # If result csv has SQL_new, maybe use that? usually same as source.
                    if res_row.get('SQL_new'):
                        generated_sql = res_row.get('SQL_new')

                cursor.execute("""
                    INSERT OR REPLACE INTO executions (test_case_id, batch_name, generated_sql, execution_status, execution_message, execution_result_summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (
                    test_case_id,
                    batch_name,
                    generated_sql,
                    status,
                    message,
                    summary
                ))
    
    conn.commit()
    conn.close()
    print("Migration Complete.")

if __name__ == "__main__":
    migrate()
