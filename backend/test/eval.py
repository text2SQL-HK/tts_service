import csv
import sys
import os
from pathlib import Path
from typing import List, Dict, Set

# Add backend directory to sys.path to allow imports
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir.parent.parent))

from backend.services.chat import run_sql, run_sql_for_new
from db_utils import get_connection

def execute_and_save_results(batch_name: str, db_id: str = "EXAMPLE_DATABASE", user_id: str = "eval_user", selected_indices: Set[int] = None):
    print(f"Processing batch: {batch_name}...")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    # Fetch test cases and executions for this batch
    query = """
        SELECT 
            tc.id as test_case_id,
            tc.question,
            e.generated_sql,
            e.execution_status
        FROM test_cases tc
        LEFT JOIN executions e ON tc.id = e.test_case_id AND e.batch_name = ?
    """
    params = [batch_name]
    
    if selected_indices:
        placeholders = ','.join('?' for _ in selected_indices)
        query += f" WHERE tc.id IN ({placeholders})"
        params.extend(selected_indices)
        
    query += " ORDER BY tc.id"
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    
    for row in rows:
        test_case_id = row['test_case_id']
        sql = row['generated_sql']
        
        if not sql:
            cursor.execute("""
                UPDATE executions 
                SET execution_status = 'Skipped', execution_message = 'No SQL found'
                WHERE test_case_id = ? AND batch_name = ?
            """, (test_case_id, batch_name))
            conn.commit()
            continue

        # Load bind variables for this test case (shared across batches)
        # Only load from DB if we are running ALL rows. 
        # If running specific rows, user likely wants to reset/re-enter bindings.
        bind_vars = {}
        if selected_indices is None:
            cursor.execute("SELECT var_key, var_value FROM bind_variables WHERE test_case_id = ?", (test_case_id,))
            bind_vars = {r['var_key']: r['var_value'] for r in cursor.fetchall()}

        # Apply bind variables
        current_sql = sql
        for key, value in bind_vars.items():
            if key in current_sql:
                current_sql = current_sql.replace(key, value)

        while True:
            try:
                # Execute SQL
                result = run_sql(sql=current_sql, db_id=db_id, user_id=user_id, save_csv=False)
                
                # Success
                res_summary = f"Rows returned: {len(result.get('rows', []))}. Columns: {', '.join(result.get('headers', []))}"
                
                cursor.execute("""
                    INSERT OR REPLACE INTO executions (test_case_id, batch_name, generated_sql, execution_status, execution_message, execution_result_summary)
                    VALUES (?, ?, ?, 'Success', 'Executed successfully', ?)
                """, (test_case_id, batch_name, sql, res_summary))
                conn.commit()
                break

            except Exception as e:
                error_msg = str(e)
                if "ORA-01008" in error_msg or "not all variables bound" in error_msg:
                    print(f"\n[!] Bind Variable Error for SQL (Row {test_case_id}):\n{current_sql}\nError: {error_msg}")
                    print("Enter bind variables one by one (format: :key=value). Enter 'done' to retry or 'skip' to give up.")
                    
                    while True:
                        user_input = input("Input (e.g., :id='123' or done/skip): ").strip()
                        if user_input.lower() == 'skip':
                            break 
                        
                        if user_input.lower() == 'done':
                            break 
                        
                        if '=' in user_input:
                            key, value = user_input.split('=', 1)
                            key = key.strip()
                            value = value.strip()
                            
                            # Update DB
                            cursor.execute("""
                                INSERT OR REPLACE INTO bind_variables (test_case_id, var_key, var_value)
                                VALUES (?, ?, ?)
                            """, (test_case_id, key, value))
                            conn.commit()
                            
                            # Update local map and SQL
                            bind_vars[key] = value
                            current_sql = current_sql.replace(key, value)
                            print(f"Updated SQL: {current_sql}")
                        else:
                            print("Invalid format. Use :key=value")
                    
                    if user_input.lower() == 'skip':
                         cursor.execute("""
                            INSERT OR REPLACE INTO executions (test_case_id, batch_name, generated_sql, execution_status, execution_message, execution_result_summary)
                            VALUES (?, ?, ?, 'Failed', ?, '')
                        """, (test_case_id, batch_name, sql, f"Skipped by user: {error_msg}"))
                         conn.commit()
                         break
                    # If done, loop repeats and tries to run again
                else:
                    # Other errors
                    cursor.execute("""
                        INSERT OR REPLACE INTO executions (test_case_id, batch_name, generated_sql, execution_status, execution_message, execution_result_summary)
                        VALUES (?, ?, ?, 'Failed', ?, '')
                    """, (test_case_id, batch_name, sql, error_msg))
                    conn.commit()
                    break

    conn.close()
    print(f"Finished processing batch: {batch_name}")

if __name__ == "__main__":
    all_batches = ["new", "local"]
    
    print("Enter row numbers to execute (1-based, comma-separated, e.g., 1,3,5) or 'all':")
    selection = input("Rows: ").strip().lower()
    
    selected_indices = None
    if selection != 'all':
        try:
            parts = [p.strip() for p in selection.split(',') if p.strip()]
            selected_indices = {int(p) for p in parts}
        except ValueError:
            print("Invalid input. Processing ALL rows.")
            selected_indices = None

    print(f"Enter batch to execute ({', '.join(all_batches)}) or 'all':")
    batch_selection = input("Batch: ").strip().lower()
    
    target_batches = []
    if batch_selection == 'all':
        target_batches = all_batches
    elif batch_selection in all_batches:
        target_batches = [batch_selection]
    else:
        print(f"Invalid batch. Defaulting to ALL ({', '.join(all_batches)})")
        target_batches = all_batches

    for batch in target_batches:
        execute_and_save_results(batch, selected_indices=selected_indices)
