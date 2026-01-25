import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent / "test_results.db"

def get_connection():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_connection()
    c = conn.cursor()
    
    # 1. Test Cases
    c.execute("""
        CREATE TABLE IF NOT EXISTS test_cases (
            id INTEGER PRIMARY KEY,
            question TEXT,
            simplified_schema TEXT,
            gold_sql TEXT
        )
    """)
    
    # 2. Executions
    c.execute("""
        CREATE TABLE IF NOT EXISTS executions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            test_case_id INTEGER,
            batch_name TEXT,
            generated_sql TEXT,
            execution_status TEXT,
            execution_message TEXT,
            execution_result_summary TEXT,
            FOREIGN KEY(test_case_id) REFERENCES test_cases(id),
            UNIQUE(test_case_id, batch_name)
        )
    """)
    
    # 3. Bind Variables
    c.execute("""
        CREATE TABLE IF NOT EXISTS bind_variables (
            test_case_id INTEGER,
            var_key TEXT,
            var_value TEXT,
            PRIMARY KEY (test_case_id, var_key),
            FOREIGN KEY(test_case_id) REFERENCES test_cases(id)
        )
    """)
    
    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print(f"Database initialized at {DB_PATH}")
