import sys
from pathlib import Path
from db_utils import get_connection

# Add backend directory to sys.path
current_dir = Path(__file__).resolve().parent

def get_stats(batch_name):
    conn = get_connection()
    cursor = conn.cursor()
    
    # Get overall stats
    cursor.execute("""
        SELECT execution_status, count(*) as cnt 
        FROM executions 
        WHERE batch_name = ?
        GROUP BY execution_status
    """, (batch_name,))
    
    rows = cursor.fetchall()
    
    stats = {
        "Total": 0,
        "Success": 0,
        "Failed": 0,
        "Skipped": 0,
        "Success Rate": 0.0,
        "Failed Rows": [],
        "ByTableCount": {} # { count: { Total, Success, Rate } }
    }
    
    for r in rows:
        status = r['execution_status']
        count = r['cnt']
        stats["Total"] += count
        if status in stats:
            stats[status] = count
            
    if stats["Total"] > 0:
        stats["Success Rate"] = (stats["Success"] / stats["Total"]) * 100
        
    # Get stats by table count
    cursor.execute("""
        SELECT 
            tc.table_count,
            e.execution_status,
            count(*) as cnt
        FROM executions e
        JOIN test_cases tc ON e.test_case_id = tc.id
        WHERE e.batch_name = ?
        GROUP BY tc.table_count, e.execution_status
        ORDER BY tc.table_count
    """, (batch_name,))
    
    table_rows = cursor.fetchall()
    for r in table_rows:
        tc = r['table_count']
        status = r['execution_status']
        cnt = r['cnt']
        
        if tc not in stats["ByTableCount"]:
            stats["ByTableCount"][tc] = {"Total": 0, "Success": 0, "Rate": 0.0}
        
        stats["ByTableCount"][tc]["Total"] += cnt
        if status == 'Success':
            stats["ByTableCount"][tc]["Success"] += cnt
            
    # Calculate rates for table counts
    for tc in stats["ByTableCount"]:
        group = stats["ByTableCount"][tc]
        if group["Total"] > 0:
            group["Rate"] = (group["Success"] / group["Total"]) * 100

    # Get failed rows details
    cursor.execute("""
        SELECT test_case_id, execution_message 
        FROM executions 
        WHERE batch_name = ? AND execution_status = 'Failed'
        ORDER BY test_case_id
    """, (batch_name,))
    
    failed_rows = cursor.fetchall()
    for r in failed_rows:
        stats["Failed Rows"].append((r['test_case_id'], r['execution_message']))
        
    conn.close()
    return stats

def main():
    stats_a = get_stats("new")
    stats_b = get_stats("local")

    print("\n" + "="*80)
    print(f"{'Metric':<20} | {'Batch A (New)':<15} | {'Batch B (Local)':<15}")
    print("-" * 80)
    
    metrics = ["Total", "Success", "Failed", "Skipped", "Success Rate"]
    
    for metric in metrics:
        val_a = stats_a[metric]
        val_b = stats_b[metric]
        
        if metric == "Success Rate":
            str_a = f"{val_a:.2f}%"
            str_b = f"{val_b:.2f}%"
        else:
            str_a = str(val_a)
            str_b = str(val_b)
            
        print(f"{metric:<20} | {str_a:<15} | {str_b:<15}")

    print("="*80)
    print(f"Batch A: new")
    print(f"Batch B: local")
    
    # Print Table Count Breakdown
    print("\n[Success Rate by Table Count]")
    
    # Find all table counts present in either batch
    all_counts = sorted(list(set(stats_a["ByTableCount"].keys()) | set(stats_b["ByTableCount"].keys())))
    
    print("-" * 80)
    print(f"{'Table Count':<15} | {'Batch A (New)':<25} | {'Batch B (Local)':<25}")
    print("-" * 80)
    
    for tc in all_counts:
        # Batch A
        if tc in stats_a["ByTableCount"]:
            s_a = stats_a["ByTableCount"][tc]
            str_a = f"{s_a['Success']}/{s_a['Total']} ({s_a['Rate']:.1f}%)"
        else:
            str_a = "-"
            
        # Batch B
        if tc in stats_b["ByTableCount"]:
            s_b = stats_b["ByTableCount"][tc]
            str_b = f"{s_b['Success']}/{s_b['Total']} ({s_b['Rate']:.1f}%)"
        else:
            str_b = "-"
            
        print(f"{str(tc) + ' Tables':<15} | {str_a:<25} | {str_b:<25}")

    # Print failure details
    print("\n[Failure Details]")
    
    def print_failures(label, failed_rows):
        print(f"\n--- {label} ---")
        if not failed_rows:
            print("No failures.")
        else:
            for row_num, error_msg in failed_rows:
                 print(f"Row {row_num}: {error_msg}")

    print_failures("Batch A (New)", stats_a["Failed Rows"])
    print_failures("Batch B (Local)", stats_b["Failed Rows"])

if __name__ == "__main__":
    main()
