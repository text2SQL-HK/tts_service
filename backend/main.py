from __future__ import annotations

import importlib.util
import sys
import docker
import asyncio
import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
import csv
import shutil
import sqlite3
import httpx
import json
from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

if __package__:
    from .state import (
        SessionData,
        _ensure_session,
        add_database,
        cleanup_inactive_sessions,
        fetch_table_snapshot,
        fetch_timeline_page,
        increment_sql_exec_count,
        record_chat_message,
        record_system_event,
        record_tool_call,
        refresh_session_context_notes,
        refresh_session_history,
        save_context_note,
        set_current_db,
    )
    from .config import APP_DIR, DB_LIST, CHAT_PATH, STATIC_DIR, SQL_ROWS_PAGE_SIZE, PIPELINE_PATH
else:
    from state import (
        SessionData,
        _ensure_session,
        add_database,
        cleanup_inactive_sessions,
        fetch_table_snapshot,
        fetch_timeline_page,
        increment_sql_exec_count,
        record_chat_message,
        record_system_event,
        record_tool_call,
        refresh_session_context_notes,
        refresh_session_history,
        save_context_note,
        set_current_db,
    )
    from config import APP_DIR, DB_LIST, CHAT_PATH, STATIC_DIR, SQL_ROWS_PAGE_SIZE, PIPELINE_PATH

from .services import tts_pipeline as _pipeline
from .services import chat as _chat

run_chat_turn = getattr(_chat, "run_chat_turn")
run_sql = getattr(_chat, "run_sql")
run_sql_for_new = getattr(_chat, "run_sql_for_new")
test_sql = getattr(_chat, "test_sql", None)
run_promode = getattr(_pipeline, "run_promode", None)
evidence_maker = getattr(_chat, "evidence_maker", None)
run_colmaker = getattr(_pipeline, "run_colmaker", None)

class PipelineInput(BaseModel):
    user_id: str
    message: str
    mode: Optional[str] = "chat"


class PipelineOutput(BaseModel):
    response: str
    history: Optional[List[Dict[str, Any]]] = None


class FollowUpInput(BaseModel):
    user_id: str
    message: str


class FollowUpOutput(BaseModel):
    response: str
    history: Optional[List[Dict[str, Any]]] = None
    img: str

class SqlExecuteInput(BaseModel):
    user_id: str
    sql: str


class SqlExecuteOutput(BaseModel):
    message: str
    headers: Optional[List[str]] = None
    rows: Optional[List[List[str]]] = None
    table_info: Optional[str] = None
    csv: Optional[str] = None
    csv_path: Optional[str] = None
    table_number: Optional[int] = None
    has_more: Optional[bool] = None
    history: Optional[List[Dict[str, Any]]] = None


class SqlJobFinalizeInput(BaseModel):
    user_id: str
    job_id: str


class SqlJobFinalizeOutput(BaseModel):
    status: str
    message: str
    history: List[Dict[str, Any]]


class SqlTableMoreInput(BaseModel):
    user_id: str
    table_number: int


class SqlTableMoreOutput(BaseModel):
    table_number: int
    headers: List[str]
    rows: List[List[str]]
    has_more: bool


class ContextQuestionInput(BaseModel):
    user_id: str
    topic: str


class ContextQuestionOutput(BaseModel):
    questions: List[str]


class ContextSaveInput(BaseModel):
    user_id: str
    topic: str
    questions: List[str]
    answers: List[str]


class ContextOutput(BaseModel):
    message: str
    topic: str
    questions: List[str]
    answers: List[str]
    evidence: str


class ContextDirectInput(BaseModel):
    user_id: str
    topic: Optional[str] = ""
    content: str


class DbSelectInput(BaseModel):
    user_id: str
    db_id: str


class DbConvertInput(BaseModel):
    user_id: str
    db_id: str
    csv_path: Optional[str] = None
    descriptions: Optional[Dict[str, Any]] = None

import re

def is_valid_user_id(user_id: str) -> bool:
    """
    Docker container name 제약 검증 함수.
    (1) [a-z0-9_.-] 문자만 사용 가능
    (2) 영문자 또는 숫자로 시작해야 함
    (3) 길이 ≤ 255
    """
    # (3) 길이 확인
    if not (1 <= len(user_id) <= 255):
        return False

    # (2) 시작 문자 확인
    if not re.match(r'^[a-z0-9]', user_id):
        return False

    # (1) 전체 문자 패턴 확인
    if not re.fullmatch(r'[a-z0-9_.-]+', user_id):
        return False

    return True

def _sanitize_db_identifier(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._-")
    return sanitized


def _normalize_sqlite_columns(headers: List[str]) -> List[str]:
    seen = set()
    normalized = []
    for index, header in enumerate(headers, start=1):
        base = re.sub(r"[^A-Za-z0-9_]+", "_", header or "").strip("_")
        if not base:
            base = f"col_{index}"
        if base[0].isdigit():
            base = f"col_{base}"
        name = base
        suffix = 1
        while name in seen:
            name = f"{base}_{suffix}"
            suffix += 1
        seen.add(name)
        normalized.append(name)
    return normalized


def _resolve_session_csv_path(
    user_id: str,
    csv_path: str,
    session_id: Optional[int] = None,
) -> Optional[Path]:
    if not csv_path:
        return None
    session_roots: List[Path] = []
    if session_id is not None:
        session_roots.append((APP_DIR / "session_data" / user_id / str(session_id)).resolve())
    session_roots.append((APP_DIR / "session_data" / user_id).resolve())
    # 과거 경로 형식(session_data/{session_id}) 호환
    if session_id is not None:
        session_roots.append((APP_DIR / "session_data" / str(session_id)).resolve())
    raw_path = Path(csv_path)
    candidates: List[Path] = []

    if raw_path.is_absolute():
        # 컨테이너 내부 /home/{session_id}/output*.csv 또는 /home/output*.csv 호환
        if str(raw_path).startswith("/home/") and len(raw_path.parts) >= 3:
            container_name = raw_path.name
            for root in session_roots:
                candidates.append(root / container_name)
            if len(raw_path.parts) >= 4:
                session_fragment = raw_path.parts[-2]
                candidates.append((APP_DIR / "session_data" / user_id / session_fragment / container_name))
        else:
            candidates.append(raw_path)
    else:
        if raw_path.parent == Path("."):
            for root in session_roots:
                candidates.append(root / raw_path.name)
        candidates.append(APP_DIR / raw_path)

    for candidate in candidates:
        resolved = candidate.resolve()
        for root in session_roots:
            if resolved == root or root in resolved.parents:
                return resolved
    return None


def _load_table_rows_from_csv(
    user_id: str,
    csv_path: str,
    session_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    resolved = _resolve_session_csv_path(user_id, csv_path, session_id=session_id)
    if resolved is None or not resolved.exists() or not resolved.is_file():
        return None
    try:
        with resolved.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            headers = next(reader, []) or []
            rows: List[List[str]] = []
            for row in reader:
                rows.append(["" if cell is None else str(cell) for cell in row])
    except OSError:
        return None
    return {
        "headers": [str(header) for header in headers],
        "rows": rows,
        "resolved_path": str(resolved),
    }


def _load_table_rows_from_container(user_id: str, csv_path: str) -> Optional[Dict[str, Any]]:
    normalized = (csv_path or "").strip()
    name = Path(normalized).name
    if not name or not name.lower().endswith(".csv"):
        return None
    in_container: Optional[str] = None
    if normalized.startswith("/home/"):
        in_container = normalized
    else:
        relative_parts = Path(normalized).parts
        if len(relative_parts) >= 4 and relative_parts[0] == "session_data":
            session_fragment = relative_parts[-2]
            in_container = f"/home/{session_fragment}/{name}"
        else:
            in_container = f"/home/{name}"
    try:
        client = docker.from_env()
        container = client.containers.get(f"c{user_id}")
        exit_code, output = container.exec_run(["cat", in_container])
        if exit_code != 0:
            return None
        csv_text = output.decode("utf-8", errors="replace")
    except Exception:
        return None

    reader = csv.reader(io.StringIO(csv_text))
    headers = next(reader, []) or []
    rows: List[List[str]] = []
    for row in reader:
        rows.append(["" if cell is None else str(cell) for cell in row])
    return {
        "headers": [str(header) for header in headers],
        "rows": rows,
        "resolved_path": in_container,
    }


def _normalize_csv_path_for_storage(
    session: SessionData,
    user_id: str,
    csv_path: Optional[str],
) -> Optional[str]:
    raw_value = (csv_path or "").strip()
    if not raw_value:
        return None
    raw_path = Path(raw_value)
    current_session_fragment = str(session.get("session_id") or "").strip() or user_id

    if raw_path.is_absolute() and raw_value.startswith("/home/"):
        file_name = raw_path.name
        if not file_name:
            return None
        session_fragment = current_session_fragment
        if len(raw_path.parts) >= 4:
            maybe_session_fragment = raw_path.parts[-2].strip()
            if maybe_session_fragment:
                session_fragment = maybe_session_fragment
        return (Path("session_data") / user_id / session_fragment / file_name).as_posix()

    if not raw_path.is_absolute():
        parts = raw_path.parts
        if len(parts) >= 4 and parts[0] == "session_data" and parts[1] == user_id:
            return raw_path.as_posix()
        if raw_path.name.lower().endswith(".csv"):
            return (Path("session_data") / user_id / current_session_fragment / raw_path.name).as_posix()

    return raw_path.as_posix()

def _visible_history(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    visible: List[Dict[str, Any]] = []
    for entry in history:
        if entry.get("role") == "system":
            continue
        if entry.get("type") == "internal":
            continue
        content = entry.get("content")
        if content == "SQL 실행해줘.":
            continue
        if isinstance(content, str) and content.startswith("SQL 실행 결과:"):
            continue
        visible.append(entry)
    return visible

def _build_sql_success_message(sql_text: str, question: Optional[str] = None) -> str:
    trimmed_sql = (sql_text or "").strip()
    description_lines = ["사용자 요청을 토대로 생성된 SQL입니다."]
    if question:
        description_lines.append(f"질문 요약: {question}")
    description = "\n".join(description_lines)
    return (
        "**SQL**\n``` sql\n"
        f"{trimmed_sql}\n"
        "```\n``` description\n"
        f"{description}\n"
        "```"
    )

def _run_chat_turn(session: SessionData, message: str, user_id: str) -> Dict[str, Any]:
    history = session["chat_history"]
    history_len_before = len(history)
    try:
        result = run_chat_turn(
            message,
            history,
            user_id,
            session_id=session.get("session_id"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"챗봇 그래프 오류: {exc}") from exc

    response_text = result.get("response", "") or ""
    image_data = result.get("image", "")
    tool_calls = result.get("tool_calls") or []

    assistant_message_id: Optional[int] = None
    for entry in history[history_len_before:]:
        role = entry.get("role")
        content = entry.get("content")
        if role in {"user", "assistant"} and isinstance(content, str):
            chat_message_id = record_chat_message(session, str(role), content)
            if role == "assistant":
                assistant_message_id = chat_message_id

    for call in tool_calls:
        if not isinstance(call, dict):
            continue
        tool_name = str(call.get("tool_name") or call.get("name") or "").strip()
        if not tool_name:
            continue
        raw_arguments = call.get("arguments")
        arguments: Dict[str, Any]
        if isinstance(raw_arguments, dict):
            arguments = raw_arguments
        elif raw_arguments is None:
            arguments = {}
        else:
            arguments = {"raw": raw_arguments}
        raw_result = call.get("result")
        result_payload: Dict[str, Any]
        if isinstance(raw_result, dict):
            result_payload = raw_result
        elif raw_result is None:
            result_payload = {}
        else:
            result_payload = {"value": raw_result}
        record_tool_call(
            session=session,
            tool_name=tool_name,
            arguments=arguments,
            result=result_payload,
            status=str(call.get("status") or "success"),
            csv_file_path=call.get("csv_file_path"),
            chat_message_id=assistant_message_id,
        )

    if image_data:
        if not history or history[-1].get("image") != image_data:
            image_entry = {"role": "assistant", "image": image_data}
            history.append(image_entry)
            record_system_event(
                session=session,
                event_type="plot",
                history_entry=image_entry,
                content={"image": image_data},
            )
    return {
        "response": response_text,
        "history": _visible_history(history),
        "img": image_data or ""
    }


app = FastAPI()

@app.on_event("startup")
async def startup_event():
    """
    백그라운드에서 주기적으로 세션 정리 작업을 수행합니다.
    """
    async def cleanup_loop():
        while True:
            try:
                # 10분마다 정리
                cleanup_inactive_sessions()
            except Exception as e:
                print(f"Session cleanup error: {e}")
            await asyncio.sleep(600)

    asyncio.create_task(cleanup_loop())

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.post("/chat", response_model=PipelineOutput)
def chat(payload: PipelineInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    if not is_valid_user_id(user_id):
        raise HTTPException(status_code=400, detail="user_id 형태가 잘못되었습니다. 영어, 숫자, _.-의 특수문자만 허용됩니다. 특수문자로 시작하면 안됩니다.")

    message = payload.message.strip()
    if not message:
        raise HTTPException(status_code=400, detail="message를 입력해 주세요.")
    
    if payload.mode == "sql":
        message = f"**SQL**\n{message}"

    session = _ensure_session(user_id)
    refresh_session_history(session, user_id)
    result = _run_chat_turn(session, message, user_id)

    payload: Dict[str, Any] = {
        "response": result["response"],
        "history": result["history"],
    }
    return payload


@app.get("/chat/history/{user_id}")
def chat_history(user_id: str):
    user_id = user_id.strip()
    if not user_id:
        return {"history": []}
    session = _ensure_session(user_id)
    refresh_session_history(session, user_id)
    return {"history": _visible_history(session.get("chat_history", []))}


@app.get("/sessions/{session_id}/timeline")
def get_session_timeline(
    session_id: int,
    before_created_at: Optional[str] = None,
    before_id: Optional[int] = None,
    limit: int = 50,
):
    if session_id <= 0:
        raise HTTPException(status_code=400, detail="session_id가 유효하지 않습니다.")
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=400, detail="limit은 1 이상 200 이하이어야 합니다.")
    if (before_created_at is None) != (before_id is None):
        raise HTTPException(
            status_code=400,
            detail="before_created_at과 before_id는 함께 전달해야 합니다.",
        )

    parsed_before_created_at: Optional[datetime] = None
    if before_created_at is not None:
        normalized = before_created_at.strip()
        if normalized.endswith("Z"):
            normalized = normalized[:-1] + "+00:00"
        try:
            parsed_before_created_at = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="before_created_at 형식이 올바르지 않습니다. ISO8601을 사용해 주세요.",
            ) from exc

    items = fetch_timeline_page(
        session_id=session_id,
        before_created_at=parsed_before_created_at,
        before_id=before_id,
        limit=limit,
    )
    next_cursor = None
    if items:
        last_item = items[-1]
        next_cursor = {
            "before_created_at": last_item.get("created_at"),
            "before_id": last_item.get("id"),
        }
    return {"items": items, "next_cursor": next_cursor}


@app.post("/chat/sql/execute", response_model=SqlExecuteOutput)
def chat_sql_execute(payload: SqlExecuteInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    sql = payload.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="sql을 입력해 주세요.")
    session = _ensure_session(user_id)
    db_id = session.get("db_id", "EXAMPLE_DATABASE")
    if db_id in DB_LIST:
        try:
            result = run_sql(sql, db_id, user_id, session_id=session.get("session_id"))
        except NotImplementedError as exc:
            return {
                "message": str(exc),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SQL 실행 중 오류: {exc}") from exc
    elif db_id in session.get("db_list", []):
        try:
            result = run_sql_for_new(sql, db_id, user_id, session_id=session.get("session_id"))
        except NotImplementedError as exc:
            return {
                "message": str(exc),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SQL 실행 중 오류: {exc}") from exc
    else:
        raise HTTPException(status_code=500, detail="SQL 실행 중 오류")
    table_number = increment_sql_exec_count(session)
    if not isinstance(result, dict):
        return {"message": "SQL 실행을 완료했습니다."}

    message = result.get("message") or "SQL 실행을 완료했습니다."
    headers = result.get("headers") or []
    rows = result.get("rows") or []
    table_info = result.get("table_info")
    csv_text = result.get("csv")
    csv_path = _normalize_csv_path_for_storage(session, user_id, result.get("csv_path"))

    page_size = SQL_ROWS_PAGE_SIZE
    visible_rows = rows[:page_size] if rows else []
    has_more = len(rows) > len(visible_rows)

    response_payload: Dict[str, Any] = {"message": message}
    if headers:
        response_payload["headers"] = headers
    response_payload["rows"] = visible_rows
    if table_info is not None:
        response_payload["table_info"] = table_info
    if csv_path:
        response_payload["csv_path"] = csv_path
    if csv_text is not None and not has_more:
        response_payload["csv"] = csv_text
    response_payload["table_number"] = table_number
    response_payload["has_more"] = has_more

    history = session["chat_history"]
    if message:
        info_entry = {"role": "assistant", "type": "text", "content": message}
        history.append(info_entry)
        record_system_event(
            session=session,
            event_type="info",
            history_entry=info_entry,
            content={"message": message},
        )

    if headers and rows:
        table_entry = {
            "role": "assistant",
            "type": "table",
            "headers": headers,
            "rows": visible_rows,
            "table_info": table_info,
            "csv": csv_text if not has_more else None,
            "table#": table_number,
            "has_more": has_more,
        }
        history.append(table_entry)
        record_system_event(
            session=session,
            event_type="table_view",
            history_entry=table_entry,
            content={
                "headers": headers,
                "rows": visible_rows,
                "table_info": table_info,
                "csv_path": csv_path,
                "table_number": table_number,
                "has_more": has_more,
            },
        )

    table_results = session.setdefault("table_results", {})
    table_record: Dict[str, Any] = {
        "headers": headers,
        "rows": rows,
        "table_info": table_info,
        "csv_path": csv_path,
        "delivered": len(visible_rows),
        "page_size": page_size,
    }
    if csv_text is not None and not has_more:
        table_record["csv"] = csv_text
    table_results[table_number] = table_record

    record_tool_call(
        session=session,
        tool_name="sql_execute",
        arguments={"sql": sql, "db_id": db_id},
        result={
            "message": message,
            "row_count": len(rows),
            "table_number": table_number,
            "has_more": has_more,
            "table_info": table_info,
        },
        status="success",
        csv_file_path=csv_path,
    )

    response_payload["history"] = _visible_history(history)
    return response_payload


@app.post("/chat/sql/test", response_model=SqlExecuteOutput)
def chat_sql_test(payload: SqlExecuteInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    if test_sql is None:
        raise HTTPException(status_code=501, detail="test_sql 함수를 찾을 수 없습니다.")

    sql = payload.sql.strip()
    if not sql:
        raise HTTPException(status_code=400, detail="sql을 입력해 주세요.")
    session = _ensure_session(user_id)
    db_id = session.get("db_id", "EXAMPLE_DATABASE")
    try:
        result = test_sql(sql, db_id, user_id, session_id=session.get("session_id"))
    except NotImplementedError as exc:
        return {
            "message": str(exc),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQL 테스트 중 오류: {exc}") from exc
    table_number = increment_sql_exec_count(session)
    if not isinstance(result, dict):
        return {"message": "SQL 테스트를 완료했습니다."}

    message = result.get("message") or "SQL 테스트를 완료했습니다."
    headers = result.get("headers") or []
    rows = result.get("rows") or []
    table_info = result.get("table_info")
    csv_text = result.get("csv")
    csv_path = _normalize_csv_path_for_storage(session, user_id, result.get("csv_path"))

    page_size = SQL_ROWS_PAGE_SIZE
    visible_rows = rows[:page_size] if rows else []
    has_more = len(rows) > len(visible_rows)

    response_payload: Dict[str, Any] = {"message": message}
    if headers:
        response_payload["headers"] = headers
    response_payload["rows"] = visible_rows
    if table_info is not None:
        response_payload["table_info"] = table_info
    if csv_path:
        response_payload["csv_path"] = csv_path
    if csv_text is not None and not has_more:
        response_payload["csv"] = csv_text
    response_payload["table_number"] = table_number
    response_payload["has_more"] = has_more

    history = session["chat_history"]
    if message:
        info_entry = {"role": "assistant", "type": "text", "content": message}
        history.append(info_entry)
        record_system_event(
            session=session,
            event_type="info",
            history_entry=info_entry,
            content={"message": message},
        )

    if headers and rows:
        table_entry = {
            "role": "assistant",
            "type": "table",
            "headers": headers,
            "rows": visible_rows,
            "table_info": table_info,
            "csv": csv_text if not has_more else None,
            "table#": table_number,
            "has_more": has_more,
        }
        history.append(table_entry)
        record_system_event(
            session=session,
            event_type="table_view",
            history_entry=table_entry,
            content={
                "headers": headers,
                "rows": visible_rows,
                "table_info": table_info,
                "csv_path": csv_path,
                "table_number": table_number,
                "has_more": has_more,
            },
        )

    table_results = session.setdefault("table_results", {})
    table_record: Dict[str, Any] = {
        "headers": headers,
        "rows": rows,
        "table_info": table_info,
        "csv_path": csv_path,
        "delivered": len(visible_rows),
        "page_size": page_size,
    }
    if csv_text is not None and not has_more:
        table_record["csv"] = csv_text
    table_results[table_number] = table_record

    record_tool_call(
        session=session,
        tool_name="sql_test",
        arguments={"sql": sql, "db_id": db_id},
        result={
            "message": message,
            "row_count": len(rows),
            "table_number": table_number,
            "has_more": has_more,
            "table_info": table_info,
        },
        status="success",
        csv_file_path=csv_path,
    )

    response_payload["history"] = _visible_history(history)
    return response_payload


@app.post("/chat/sql/table-more", response_model=SqlTableMoreOutput)
def chat_sql_table_more(payload: SqlTableMoreInput) -> SqlTableMoreOutput:
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    table_number = payload.table_number
    session = _ensure_session(user_id)
    table_results = session.setdefault("table_results", {})
    table_record = table_results.get(table_number)
    if not table_record:
        snapshot = fetch_table_snapshot(session, table_number)
        if not snapshot:
            raise HTTPException(status_code=404, detail="요청한 테이블 데이터를 찾을 수 없습니다.")

        snapshot_rows = snapshot.get("rows") or []
        snapshot_headers = snapshot.get("headers") or []
        snapshot_csv_path = snapshot.get("csv_path") or ""
        snapshot_table_info = snapshot.get("table_info")
        snapshot_has_more = bool(snapshot.get("has_more"))

        csv_table: Optional[Dict[str, Any]] = None
        session_id = session.get("session_id")
        parsed_session_id: Optional[int] = None
        try:
            if session_id is not None:
                parsed_session_id = int(session_id)
        except Exception:
            parsed_session_id = None
        if snapshot_csv_path:
            csv_table = _load_table_rows_from_csv(
                user_id,
                snapshot_csv_path,
                session_id=parsed_session_id,
            )
            if csv_table is None:
                csv_table = _load_table_rows_from_container(user_id, snapshot_csv_path)

        if csv_table is not None:
            all_rows = csv_table.get("rows") or []
            headers = csv_table.get("headers") or snapshot_headers
            if snapshot_rows:
                delivered = min(len(snapshot_rows), len(all_rows))
            else:
                delivered = SQL_ROWS_PAGE_SIZE if snapshot_has_more else len(all_rows)
                delivered = min(delivered, len(all_rows))
            has_more = delivered < len(all_rows)
            table_record = {
                "headers": headers,
                "rows": all_rows,
                "table_info": snapshot_table_info,
                "csv_path": snapshot_csv_path,
                "delivered": delivered,
                "page_size": SQL_ROWS_PAGE_SIZE,
                "has_more": has_more,
            }
        else:
            # CSV를 찾지 못하면 snapshot에 남은 rows까지만 제공
            table_record = {
                "headers": snapshot_headers,
                "rows": snapshot_rows,
                "table_info": snapshot_table_info,
                "csv_path": snapshot_csv_path or None,
                "delivered": len(snapshot_rows),
                "page_size": SQL_ROWS_PAGE_SIZE,
                "has_more": snapshot_has_more,
                "source_limited": bool(snapshot_has_more),
            }
        table_results[table_number] = table_record

    rows: List[List[str]] = table_record.get("rows") or []
    headers: List[str] = table_record.get("headers") or []
    delivered = int(table_record.get("delivered", 0))
    page_size = int(table_record.get("page_size", SQL_ROWS_PAGE_SIZE))

    if delivered >= len(rows):
        if table_record.get("source_limited"):
            raise HTTPException(
                status_code=409,
                detail="원본 CSV를 찾을 수 없어 추가 행을 불러올 수 없습니다.",
            )
        table_record["has_more"] = False
        history = session["chat_history"]
        for entry in reversed(history):
            if entry.get("type") == "table" and entry.get("table#") == table_number:
                entry["has_more"] = False
                break
        return SqlTableMoreOutput(
            table_number=table_number,
            headers=headers,
            rows=[],
            has_more=False,
        )

    next_chunk = rows[delivered : delivered + page_size]
    new_delivered = delivered + len(next_chunk)
    table_record["delivered"] = new_delivered
    has_more = new_delivered < len(rows)
    table_record["has_more"] = has_more

    history = session["chat_history"]
    for entry in reversed(history):
        if entry.get("type") == "table" and entry.get("table#") == table_number:
            entry_rows = entry.setdefault("rows", [])
            entry_rows.extend(next_chunk)
            entry["has_more"] = has_more
            break

    normalized_chunk = [
        ["" if cell is None else str(cell) for cell in row]
        for row in next_chunk
    ]

    return SqlTableMoreOutput(
        table_number=table_number,
        headers=headers,
        rows=normalized_chunk,
        has_more=has_more,
    )

@app.post("/chat/context/questions", response_model=ContextQuestionOutput)
def generate_context_questions(payload: ContextQuestionInput):
    if not callable(run_promode):
        raise HTTPException(status_code=503, detail="질문 생성 기능이 비활성화되었습니다.")

    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    topic = payload.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic을 입력해 주세요.")

    session = _ensure_session(user_id)
    try:
        questions = run_promode(topic, session["db_id"])
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"추가 질문 생성 실패: {exc}") from exc

    sanitized = [
        question.strip()
        for question in (questions or [])
        if isinstance(question, str) and question.strip()
    ]
    top_three = sanitized[:3]
    if not top_three:
        raise HTTPException(status_code=500, detail="생성된 질문이 없습니다.")

    return {"questions": top_three}


@app.post("/chat/context", response_model=ContextOutput)
def save_context(payload: ContextSaveInput):
    if not callable(evidence_maker):
        raise HTTPException(status_code=503, detail="evidence_maker가 구성되지 않았습니다.")

    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    topic = payload.topic.strip()
    if not topic:
        raise HTTPException(status_code=400, detail="topic을 입력해 주세요.")

    raw_questions = payload.questions or []
    raw_answers = payload.answers or []

    questions = [
        question.strip()
        for question in raw_questions
        if isinstance(question, str) and question.strip()
    ]
    answers = [
        answer.strip()
        for answer in raw_answers
        if isinstance(answer, str) and answer.strip()
    ]

    if not questions or not answers:
        raise HTTPException(status_code=400, detail="질문과 답변을 모두 입력해 주세요.")
    if len(questions) != len(answers):
        raise HTTPException(status_code=400, detail="질문 수와 답변 수가 일치하지 않습니다.")

    try:
        evidence = evidence_maker(questions, answers)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"QA 증거 생성 실패: {exc}") from exc

    session = _ensure_session(user_id)
    save_context_note(
        session=session,
        note_type="qa",
        topic=topic,
        content={
            "questions": questions,
            "answers": answers,
            "evidence": evidence,
        },
    )
    return {
        "message": "추가 정보를 저장했습니다.",
        "topic": topic,
        "questions": questions,
        "answers": answers,
        "evidence": evidence,
    }


@app.post("/chat/context/direct", response_model=ContextOutput)
def save_context_direct(payload: ContextDirectInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    topic = (payload.topic or "").strip() or "직접 입력"
    content = payload.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content를 입력해 주세요.")

    session = _ensure_session(user_id)
    save_context_note(
        session=session,
        note_type="text",
        topic=topic,
        content={
            "content": content,
            "evidence": content,
        },
    )
    return {
        "message": "추가 정보를 저장했습니다.",
        "topic": topic,
        "questions": [],
        "answers": [],
        "evidence": content,
    }


@app.get("/chat/context/{user_id}")
def read_context_notes(user_id: str):
    trimmed = user_id.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    session = _ensure_session(trimmed)
    refresh_session_context_notes(session)
    notes = session.get("context_notes", [])
    return {"notes": notes}


@app.get("/chat/db/list")
def read_db_list(user_id: Optional[str] = None):
    trimmed = (user_id or "").strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    session = _ensure_session(trimmed)
    return {"databases": session.get("db_list", [])}


@app.get("/chat/db/{user_id}")
def read_current_db(user_id: str):
    trimmed = user_id.strip()
    if not trimmed:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    session = _ensure_session(trimmed)
    return {"db_id": session.get("db_id", "")}


@app.post("/chat/db/select")
def select_db(payload: DbSelectInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")

    db_id = payload.db_id.strip()
    if not db_id:
        raise HTTPException(status_code=400, detail="db_id를 입력해 주세요.")
    session = _ensure_session(user_id)
    db_list = session.get("db_list", [])
    if db_id not in db_list:
        raise HTTPException(status_code=400, detail="유효하지 않은 DB입니다.")
    set_current_db(session, db_id)
    return {"message": "DB를 변경했습니다.", "db_id": db_id}


@app.post("/chat/db/upload")
def upload_db_csv(
    user_id: str = Form(...),
    file: UploadFile = File(...),
    db_id: Optional[str] = Form(None),
):
    trimmed_user_id = user_id.strip()
    if not trimmed_user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    if not is_valid_user_id(trimmed_user_id):
        raise HTTPException(status_code=400, detail="user_id 형태가 잘못되었습니다.")
    filename = (file.filename or "").strip()
    if not filename:
        raise HTTPException(status_code=400, detail="CSV 파일 이름이 비어 있습니다.")
    if not filename.lower().endswith(".csv"):
        raise HTTPException(status_code=400, detail="CSV 파일만 업로드할 수 있습니다.")

    requested_db_id = (db_id or Path(filename).stem).strip()
    safe_db_id = _sanitize_db_identifier(requested_db_id)
    if not safe_db_id:
        safe_db_id = f"csv_{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

    upload_root = APP_DIR / "uploads" / trimmed_user_id / safe_db_id
    upload_root.mkdir(parents=True, exist_ok=True)

    base_name = _sanitize_db_identifier(Path(filename).stem) or "data"
    safe_filename = f"{base_name}.csv"
    destination = upload_root / safe_filename
    if destination.exists():
        timestamp = datetime.utcnow().strftime("%Y%m%d%H%M%S")
        destination = upload_root / f"{base_name}_{timestamp}.csv"

    with destination.open("wb") as target:
        shutil.copyfileobj(file.file, target)
    file.file.close()

    relative_path = destination
    try:
        relative_path = destination.relative_to(APP_DIR)
    except ValueError:
        pass

    return {
        "message": "CSV 업로드를 완료했습니다.",
        "db_id": safe_db_id,
        "path": str(relative_path),
        "registered": False,
    }


@app.post("/chat/db/convert")
def convert_db_csv(payload: DbConvertInput):
    user_id = payload.user_id.strip()
    if not user_id:
        raise HTTPException(status_code=400, detail="user_id를 입력해 주세요.")
    if not is_valid_user_id(user_id):
        raise HTTPException(status_code=400, detail="user_id 형태가 잘못되었습니다.")

    db_id = payload.db_id.strip()
    if not db_id:
        raise HTTPException(status_code=400, detail="db_id를 입력해 주세요.")
    safe_db_id = _sanitize_db_identifier(db_id)
    if not safe_db_id:
        raise HTTPException(status_code=400, detail="db_id가 유효하지 않습니다.")

    base_dir = APP_DIR / "uploads" / user_id / safe_db_id
    if payload.csv_path:
        candidate = Path(payload.csv_path)
        if not candidate.is_absolute():
            candidate = APP_DIR / candidate
        csv_path = candidate.resolve()
        if not csv_path.exists():
            raise HTTPException(status_code=404, detail="CSV 파일을 찾을 수 없습니다.")
        if base_dir not in csv_path.parents:
            raise HTTPException(status_code=400, detail="CSV 경로가 올바르지 않습니다.")
        if csv_path.suffix.lower() != ".csv":
            raise HTTPException(status_code=400, detail="CSV 파일만 변환할 수 있습니다.")
    else:
        if not base_dir.exists():
            raise HTTPException(status_code=404, detail="CSV 업로드 경로를 찾을 수 없습니다.")
        csv_files = sorted(
            base_dir.glob("*.csv"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not csv_files:
            raise HTTPException(status_code=404, detail="CSV 파일을 찾을 수 없습니다.")
        csv_path = csv_files[0]
    
    sqlite_path = base_dir / f"{safe_db_id}.sqlite"
    if sqlite_path.exists():
        raise HTTPException(status_code=409, detail="이미 변환된 SQLite 파일이 있습니다.")

    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        headers = next(reader, None)
        if not headers:
            raise HTTPException(status_code=400, detail="CSV 헤더를 찾을 수 없습니다.")
        headers = [header.strip() for header in headers]
        column_samples = [[] for _ in headers]
        sampled_rows: List[List[Any]] = []
        MAX_SAMPLES = 3
        for row in reader:
            sampled_rows.append(row)
            for i, value in enumerate(row):
                if i >= len(column_samples):
                    continue
                if len(column_samples[i]) < MAX_SAMPLES:
                    column_samples[i].append(value)
            if all(len(col) >= MAX_SAMPLES for col in column_samples):
                break
        raw_descriptions = payload.descriptions or {}
        table_description = ""
        column_descriptions = [""] * len(headers)
        if isinstance(raw_descriptions, dict) and (
            "table" in raw_descriptions or "column" in raw_descriptions
        ):
            table_description = str(raw_descriptions.get("table", "") or "")
            raw_columns = raw_descriptions.get("column", [])
            if isinstance(raw_columns, list):
                for idx in range(len(headers)):
                    if idx < len(raw_columns):
                        column_descriptions[idx] = (
                            "" if raw_columns[idx] is None else str(raw_columns[idx])
                        )
        elif isinstance(raw_descriptions, dict):
            for idx, header in enumerate(headers):
                column_descriptions[idx] = str(raw_descriptions.get(header, "") or "")

        llm_input_list = []
        for i, header in enumerate(headers):
            samples = column_samples[i]
            description = column_descriptions[i]
            llm_input_list.append(f"{header} | {description} | Example Values: {', '.join(samples)}")
        columns = _normalize_sqlite_columns(run_colmaker(llm_input_list))
        if not columns or len(columns) != len(headers):
            raise HTTPException(status_code=500, detail="열 이름 생성에 실패했습니다.")
        
        table_name = _sanitize_db_identifier(safe_db_id) or "data"

        conn = sqlite3.connect(sqlite_path)
        try:
            cur = conn.cursor()
            columns_def = ", ".join(f"\"{col}\" TEXT" for col in columns)
            cur.execute(f"CREATE TABLE \"{table_name}\" ({columns_def});")
            placeholders = ", ".join(["?"] * len(columns))
            column_list = ", ".join(f"\"{col}\"" for col in columns)
            insert_sql = f"INSERT INTO \"{table_name}\" ({column_list}) VALUES ({placeholders})"

            def _normalize_row(row: List[Any]) -> List[Any]:
                if len(row) < len(columns):
                    return row + [None] * (len(columns) - len(row))
                if len(row) > len(columns):
                    return row[: len(columns)]
                return row

            batch = []
            for row in sampled_rows:
                batch.append(_normalize_row(row))
                if len(batch) >= 1000:
                    cur.executemany(insert_sql, batch)
                    batch = []
            for row in reader:
                batch.append(_normalize_row(row))
                if len(batch) >= 1000:
                    cur.executemany(insert_sql, batch)
                    batch = []
            if batch:
                cur.executemany(insert_sql, batch)
            conn.commit()
        finally:
            conn.close()
    desc_list: List[str] = []
    desc_list.append(f"#Table: {table_name}\n{table_description}\nColumns\n")
    desc_list.append("[\n")
    for idx in range(len(headers)):
        desc_list.append(f"{columns[idx]}은(는) {column_descriptions[idx]}을 의미한다. 예시 값으로는 {', '.join(column_samples[idx])} 등이 있다.\n")
    desc_list.append("]\n")
    desc_str = "".join(desc_list)
    description_path = base_dir / f"{safe_db_id}_description.txt"
    with description_path.open("w", encoding="utf-8") as desc_handle:
        desc_handle.write(desc_str)
    relative_sqlite_path = sqlite_path
    try:
        relative_sqlite_path = sqlite_path.relative_to(APP_DIR)
    except ValueError:
        pass
    
    session = _ensure_session(user_id)
    add_database(session, safe_db_id, set_current=True)
    return {
        "message": "CSV를 SQLite로 변환했습니다.",
        "db_id": safe_db_id,
        "sqlite_path": str(relative_sqlite_path),
        "table": table_name,
        "columns": columns,
    }


@app.get("/")
def read_root():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(
            status_code=503,
            detail="정적 UI 리소스를 찾을 수 없습니다. 'static/index.html'을 확인하세요.",
        )
    return FileResponse(index_path)


@app.get("/api/info")
def api_info():
    return {
        "message": "SQL 파이프라인 없이 바로 대화하는 챗봇입니다.",
        "endpoints": {
            "POST /chat": "대화를 시작합니다.",
            "POST /chat/followup": "대화를 이어갑니다.",
            "GET /chat/history/{user_id}": "현재까지의 대화 이력을 반환합니다.",
            "GET /api/info": "API 정보를 제공합니다.",
        },
    }
