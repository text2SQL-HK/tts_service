import csv
import sys
from pathlib import Path
from db_utils import get_connection
from eval import execute_and_save_results

current_dir = Path(__file__).resolve().parent

def run_and_export():
    print("Running execution for Batch A ('new') using saved bind variables...")
    
    # 1. Execute Batch A (None = run all rows, which triggers loading bind_vars from DB)
    try:
        execute_and_save_results("new", selected_indices=None)
    except Exception as e:
        print(f"Error during execution: {e}")
        # Continue to export whatever is there
    
    # 2. Export Results to CSV
    output_csv = current_dir / "batch_a_results.csv"
    print(f"\nExporting results to {output_csv}...")
    
    conn = get_connection()
    cursor = conn.cursor()
    
    query = """
        SELECT 
            tc.id,
            tc.question,
            tc.table_count,
            tc.table_names,
            e.generated_sql,
            e.execution_status,
            e.execution_message,
            e.execution_result_summary
        FROM test_cases tc
        LEFT JOIN executions e ON tc.id = e.test_case_id AND e.batch_name = 'new'
        ORDER BY tc.id
    """
    
    cursor.execute(query)
    rows = cursor.fetchall()
    
    with open(output_csv, 'w', encoding='utf-8', newline='') as f:
        # Get column names from cursor description
        # headers = [description[0] for description in cursor.description]
        # Custom headers for clarity
        headers = [
            "ID", "Question", "Table Count", "Table Names", 
            "Generated SQL", "Status", "Message", "Result Summary"
        ]
        
        writer = csv.writer(f)
        writer.writerow(headers)
        
        for row in rows:
            writer.writerow(list(row))
            
    conn.close()
    print("Export complete.")

if __name__ == "__main__":
    run_and_export()
