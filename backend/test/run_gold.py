import sys
from pathlib import Path
from db_utils import get_connection
# Add backend directory to sys.path to allow imports from eval
current_dir = Path(__file__).resolve().parent
sys.path.append(str(current_dir))
from eval import execute_and_save_results

def run_gold():
    print("Preparing Gold SQL batch...")
    
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

    conn = get_connection()
    cursor = conn.cursor()
    
    # 1. Populate 'executions' table with Gold SQL for the selected rows (or all)
    # We copy gold_sql from test_cases to generated_sql in executions for batch 'gold'
    
    # Delete existing gold executions for these rows to avoid conflict
    del_query = "DELETE FROM executions WHERE batch_name = 'gold'"
    del_params = []
    if selected_indices:
        placeholders = ','.join('?' for _ in selected_indices)
        del_query += f" AND test_case_id IN ({placeholders})"
        del_params = list(selected_indices)
    
    cursor.execute(del_query, del_params)

    # Insert new
    query = """
        INSERT INTO executions (test_case_id, batch_name, generated_sql, execution_status, execution_message, execution_result_summary)
        SELECT id, 'gold', gold_sql, 'Skipped', 'Ready for execution', ''
        FROM test_cases
    """
    params = []
    if selected_indices:
        placeholders = ','.join('?' for _ in selected_indices)
        query += f" WHERE id IN ({placeholders})"
        params = list(selected_indices)
    
    cursor.execute(query, params)
    conn.commit()
    conn.close()
    
    print("Gold batch prepared. Starting execution...")
    print("(You will be prompted to enter bind variables for new parameters like :B1, :B2)")
    
    # 2. Execute using eval.py logic
    # We pass selected_indices so that eval.py knows to SKIP loading bind vars if specific rows were selected,
    # or load them if 'all' was selected.
    # Note: If it's the first time running gold, bind vars won't exist anyway, so prompting is guaranteed.
    execute_and_save_results("gold", selected_indices=selected_indices)

if __name__ == "__main__":
    run_gold()
