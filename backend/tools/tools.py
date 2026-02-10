import docker
import re

from ..state import _ensure_session, refresh_session_context_notes
from ..config import PIPELINE_PATH, APP_DIR, DB_LIST

from ..services import tts_pipeline as _pipeline

run_direct = getattr(_pipeline, "run_direct")
run_direct_for_new = getattr(_pipeline, "run_direct_for_new")

# --- 도구 정의 -------------------------------------------------------------------
def normalize_output(output):
    if isinstance(output, (bytes, bytearray)):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _session_fragment(user_id: str, session_id: str | None = None) -> str:
    raw = (session_id or "").strip() or user_id.strip()
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw).strip("._-")
    return sanitized or "default"


def _session_work_dir(user_id: str, session_id: str | None = None):
    root = APP_DIR / "session_data" / user_id
    root.mkdir(parents=True, exist_ok=True)
    fragment = _session_fragment(user_id, session_id)
    session_dir = root / fragment
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir, fragment

# --- 도구 메타데이터 정의 ------------------------------------------------------------------- 
TOOLS = [
    {
        "type": "function",
        "name": "plot",
        "description": "Execute the python code(Matplotlib) and draw the plot in the Docker container. The container is identified by the user_id. ",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID to identify the Docker container",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID used for per-session workspace directory.",
                },
            },
            "required": ["code", "user_id"],
        },
    },
    {
        "type": "function",
        "name": "execute_python",
        "description": "Execute the python code in the Docker container. The container is identified by the user_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute",
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID to identify the Docker container",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID used for per-session workspace directory.",
                },
            },
            "required": ["code", "user_id"],
        },
    },
    {
        "type": "function",
        "name": "text_to_sql",
        "description": "Execute the python code(Matplotlib) and draw the plot in the Docker container. The container is identified by the user_id. ",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Query to convert to SQL",
                },
                "user_id": {
                    "type": "string",
                    "description": "User ID to identify the Docker container",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID used for per-session workspace directory.",
                },
            },
            "required": ["query", "user_id"],
        },
    },
]

def plot(code: str, user_id: str, session_id: str | None = None) -> str:
    print(f"Plot Code for {user_id}:\n{code}")
    client = docker.from_env()
    try: 
        container = client.containers.get(f"c{user_id}")

        session_dir, fragment = _session_work_dir(user_id, session_id)
        script_path = session_dir / "temporary.py"
        script_path.write_text(code, encoding="utf-8")

        container_script_path = f"/home/{fragment}/temporary.py"
        container_workdir = f"/home/{fragment}"
        exit_code, output = container.exec_run(
            ["python", container_script_path],
            workdir=container_workdir,
        )
        
        if exit_code != 0:
            error_msg = normalize_output(output)
            print(f"Plot execution failed: {error_msg}")
            return f"failed by execution error: {error_msg}"
            
        return "success"
    except Exception as e:
         return f"failed by {e}"

def execute_python(code: str, user_id: str, session_id: str | None = None) -> str:
    client = docker.from_env()
    # 컨테이너 가져오기
    try:
        container = client.containers.get(f"c{user_id}")
    except docker.errors.NotFound:
        return "Container not found. Please refresh the session."

    session_dir, fragment = _session_work_dir(user_id, session_id)

    script_path = session_dir / "temporary.py"
    script_path.write_text(code, encoding="utf-8")

    # 컨테이너 내부 경로 /home/{session_id}/temporary.py 실행
    container_script_path = f"/home/{fragment}/temporary.py"
    container_workdir = f"/home/{fragment}"
    exit_code, output = container.exec_run(
        ["python", container_script_path],
        workdir=container_workdir,
    )
    
    # 실행 후 파일 삭제 (선택사항)
    # script_path.unlink(missing_ok=True)

    return normalize_output(output)

def text_to_sql(query: str, user_id: str, session_id: str | None = None) -> str:
    print(query)
    session = _ensure_session(user_id)
    refresh_session_context_notes(session)
    db_id = session.get("db_id", "EXAMPLE_DATABASE")
    evidence = session.get("evidence", "")
    result = run_direct(question=query, evidence=evidence, db_id=db_id)
    if db_id not in DB_LIST and db_id in session.get("db_list", []):
        result = run_direct_for_new(question=query, evidence=evidence, db_id=db_id, user_id=user_id)
    return str(result)
