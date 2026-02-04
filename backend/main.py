from __future__ import annotations

import importlib.util
import sys
import docker
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
    from .state import SessionData, SESSIONS, _ensure_session
    from .config import APP_DIR, DB_LIST, CHAT_PATH, STATIC_DIR, SQL_ROWS_PAGE_SIZE, PIPELINE_PATH
else:
    from state import SessionData, SESSIONS, _ensure_session
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
    try:
        result = run_chat_turn(message, history, user_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"챗봇 그래프 오류: {exc}") from exc

    response_text = result.get("response", "") or ""
    image_data = result.get("image", "")
    if image_data:
        if not history or history[-1].get("image") != image_data:
            history.append(
                {
                    "role": "assistant",
                    "image": image_data,
                },
            )
    return {
        "response": response_text,
        "history": _visible_history(history),
        "img": image_data or ""
    }


app = FastAPI()
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
    session = SESSIONS.get(user_id)
    if not session:
        return {"history": []}
    return {"history": _visible_history(session.get("chat_history", []))}

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
            result = run_sql(sql, db_id, user_id)
        except NotImplementedError as exc:
            return {
                "message": str(exc),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SQL 실행 중 오류: {exc}") from exc
    elif db_id in session.get("db_id", []):
        try:
            result = run_sql_for_new(sql, db_id, user_id)
        except NotImplementedError as exc:
            return {
                "message": str(exc),
            }
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"SQL 실행 중 오류: {exc}") from exc
    else: HTTPException(status_code=500, detail="SQL 실행 중 오류")
    session["sql_exec#"] += 1
    if not isinstance(result, dict):
        return {"message": "SQL 실행을 완료했습니다."}

    message = result.get("message") or "SQL 실행을 완료했습니다."
    headers = result.get("headers") or []
    rows = result.get("rows") or []
    table_info = result.get("table_info")
    csv_text = result.get("csv")
    csv_path = result.get("csv_path")

    page_size = SQL_ROWS_PAGE_SIZE
    visible_rows = rows[:page_size] if rows else []
    has_more = len(rows) > len(visible_rows)
    table_number = session["sql_exec#"]

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

    session = _ensure_session(user_id)
    history = session["chat_history"]
    if message:
        history.append(
            {
                "role": "assistant",
                "type": "text",
                "content": message,
            },
        )

    if headers and rows:
        history.append(
            {
                "role": "assistant",
                "type": "table",
                "headers": headers,
                "rows": visible_rows,
                "table_info": table_info,
                "csv": csv_text if not has_more else None,
                "table#": table_number,
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
        result = test_sql(sql, db_id, user_id)
    except NotImplementedError as exc:
        return {
            "message": str(exc),
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"SQL 테스트 중 오류: {exc}") from exc
    session["sql_exec#"] += 1
    if not isinstance(result, dict):
        return {"message": "SQL 테스트를 완료했습니다."}

    message = result.get("message") or "SQL 테스트를 완료했습니다."
    headers = result.get("headers") or []
    rows = result.get("rows") or []
    table_info = result.get("table_info")
    csv_text = result.get("csv")
    csv_path = result.get("csv_path")

    page_size = SQL_ROWS_PAGE_SIZE
    visible_rows = rows[:page_size] if rows else []
    has_more = len(rows) > len(visible_rows)
    table_number = session["sql_exec#"]

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

    session = _ensure_session(user_id)
    history = session["chat_history"]
    if message:
        history.append(
            {
                "role": "assistant",
                "type": "text",
                "content": message,
            },
        )

    if headers and rows:
        history.append(
            {
                "role": "assistant",
                "type": "table",
                "headers": headers,
                "rows": visible_rows,
                "table_info": table_info,
                "csv": csv_text if not has_more else None,
                "table#": table_number,
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
        raise HTTPException(status_code=404, detail="요청한 테이블 데이터를 찾을 수 없습니다.")

    rows: List[List[str]] = table_record.get("rows") or []
    headers: List[str] = table_record.get("headers") or []
    delivered = int(table_record.get("delivered", 0))
    page_size = int(table_record.get("page_size", SQL_ROWS_PAGE_SIZE))

    if delivered >= len(rows):
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
    notes = session.setdefault("context_notes", [])
    created_at = datetime.utcnow().isoformat() + "Z"
    notes.append(
        {
            "type": "qa",
            "topic": topic,
            "questions": questions,
            "answers": answers,
            "evidence": evidence,
            "created_at": created_at,
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
    notes = session.setdefault("context_notes", [])
    created_at = datetime.utcnow().isoformat() + "Z"
    note = {
        "type": "text",
        "topic": topic,
        "content": content,
        "questions": [],
        "answers": [],
        "evidence": content,
        "created_at": created_at,
    }
    notes.append(note)
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
    session["db_id"] = db_id
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
    session["db_id"] = safe_db_id
    session["db_list"].append(safe_db_id)
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
