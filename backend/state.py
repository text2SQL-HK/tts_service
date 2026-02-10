from __future__ import annotations

from datetime import datetime, timezone
import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

import docker
try:
    import psycopg2
    from psycopg2.extras import Json
    from psycopg2.pool import ThreadedConnectionPool
except Exception:  # pragma: no cover - runtime environment dependent
    psycopg2 = None
    Json = None
    ThreadedConnectionPool = None

try:
    import redis
    from redis.exceptions import RedisError
except Exception:  # pragma: no cover - runtime environment dependent
    redis = None

    class RedisError(Exception):
        pass

from .config import (
    APP_DIR,
    CACHE_EVENTS_LIMIT,
    CACHE_HISTORY_LIMIT,
    CACHE_TTL_SECONDS,
    CHATBOT_SYS_PROMPT,
    DB_LIST,
    PLOT_IMAGE_NAME,
    POSTGRE_HOST,
    POSTGRE_NAME,
    POSTGRE_POOL_MAX_CONN,
    POSTGRE_POOL_MIN_CONN,
    POSTGRE_PORT,
    POSTGRE_PWD,
    POSTGRE_USER,
    REDIS_DB,
    REDIS_HOST,
    REDIS_PASSWORD,
    REDIS_PORT,
    SESSION_TIMEOUT_SECONDS,
)

class SessionData(TypedDict, total=False):
    session_id: int
    chat_history: List[Dict[str, Any]]
    context_notes: List[Dict[str, Any]]
    db_id: str
    db_list: List[str]
    evidence: str
    last_accessed: float

SESSIONS: Dict[str, SessionData] = {}

def _to_utc_iso(value: datetime) -> str:
    dt = value.astimezone(timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def _coerce_datetime(value: Any) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            pass
    return datetime.now(timezone.utc)


def _decode_json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return value
    return value


def _normalize_db_list(values: Any, fallback: Optional[List[str]] = None) -> List[str]:
    parsed = _decode_json_value(values)
    source = parsed if isinstance(parsed, list) else (fallback or [])
    normalized: List[str] = []
    seen = set()
    for item in source:
        candidate = str(item).strip()
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        normalized.append(candidate)
    return normalized


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _safe_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "y"}:
            return True
        if lowered in {"false", "0", "no", "n"}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return default


class _PersistentStore:
    def __init__(self) -> None:
        self._redis_client: Optional["redis.Redis"] = None
        self._redis_disabled = False
        self._pg_pool: Optional["ThreadedConnectionPool"] = None
        self._pg_pool_disabled = False

    def _get_pg_pool(self) -> Optional["ThreadedConnectionPool"]:
        if self._pg_pool_disabled or ThreadedConnectionPool is None:
            return None
        if self._pg_pool is not None:
            return self._pg_pool
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary 패키지가 설치되지 않았습니다.")
        if not POSTGRE_NAME:
            raise RuntimeError("POSTGRE_NAME(또는 POSTGRE_DB)이 설정되지 않았습니다.")
        try:
            self._pg_pool = ThreadedConnectionPool(
                minconn=max(1, POSTGRE_POOL_MIN_CONN),
                maxconn=max(POSTGRE_POOL_MIN_CONN, POSTGRE_POOL_MAX_CONN),
                host=POSTGRE_HOST,
                dbname=POSTGRE_NAME,
                user=POSTGRE_USER,
                password=POSTGRE_PWD,
                port=POSTGRE_PORT,
                connect_timeout=5,
            )
            return self._pg_pool
        except Exception as exc:
            print(f"[PostgreSQL] connection pool 생성 실패. direct connect로 동작합니다: {exc}")
            self._pg_pool_disabled = True
            return None

    def _connect_direct(self):
        if psycopg2 is None:
            raise RuntimeError("psycopg2-binary 패키지가 설치되지 않았습니다.")
        if not POSTGRE_NAME:
            raise RuntimeError("POSTGRE_NAME(또는 POSTGRE_DB)이 설정되지 않았습니다.")
        return psycopg2.connect(
            host=POSTGRE_HOST,
            dbname=POSTGRE_NAME,
            user=POSTGRE_USER,
            password=POSTGRE_PWD,
            port=POSTGRE_PORT,
            connect_timeout=5,
        )

    @contextmanager
    def _pg_connect(self):
        conn = None
        pool = self._get_pg_pool()
        from_pool = False
        try:
            if pool is not None:
                conn = pool.getconn()
                from_pool = True
            else:
                conn = self._connect_direct()
            yield conn
            conn.commit()
        except Exception:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is None:
                return
            if from_pool and pool is not None:
                pool.putconn(conn)
            else:
                conn.close()

    def _get_redis(self) -> Optional["redis.Redis"]:
        if redis is None:
            return None
        if self._redis_disabled:
            return None
        if self._redis_client is not None:
            return self._redis_client
        try:
            client = redis.Redis(
                host=REDIS_HOST,
                port=REDIS_PORT,
                db=REDIS_DB,
                password=REDIS_PASSWORD or None,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2,
            )
            client.ping()
            self._redis_client = client
            return client
        except RedisError as exc:
            print(f"[Redis] 연결 실패. DB-only 모드로 동작합니다: {exc}")
            self._redis_disabled = True
            return None

    @staticmethod
    def _cache_keys(session_id: int) -> Dict[str, str]:
        prefix = f"session:{session_id}"
        return {
            "meta": f"{prefix}:meta",
            "history": f"{prefix}:history",
            "events": f"{prefix}:events",
            "version": f"{prefix}:version",
        }

    @staticmethod
    def _to_int(value: Any, default: int = 0) -> int:
        return _safe_int(value, default)

    @staticmethod
    def _serialize_chat_cache_entry(row: Dict[str, Any]) -> Dict[str, Any]:
        created_at = _coerce_datetime(row.get("created_at"))
        return {
            "id": int(row.get("id") or row.get("chat_message_id") or 0),
            "chat_message_id": int(row.get("id") or row.get("chat_message_id") or 0),
            "role": str(row.get("role") or ""),
            "content": str(row.get("content") or ""),
            "created_at": _to_utc_iso(created_at),
        }

    @staticmethod
    def _serialize_event_cache_entry(row: Dict[str, Any]) -> Dict[str, Any]:
        created_at = _coerce_datetime(row.get("created_at"))
        return {
            "id": int(row.get("id") or row.get("system_event_id") or 0),
            "system_event_id": int(row.get("id") or row.get("system_event_id") or 0),
            "event_type": str(row.get("event_type") or ""),
            "content": row.get("content"),
            "created_at": _to_utc_iso(created_at),
        }

    @staticmethod
    def _deserialize_cache_entries(raw_entries: List[str]) -> List[Dict[str, Any]]:
        parsed: List[Dict[str, Any]] = []
        for raw in raw_entries:
            payload = _decode_json_value(raw)
            if isinstance(payload, dict):
                parsed.append(payload)
        return parsed

    def _compute_db_stats(self, session_id: int) -> Dict[str, int]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT
                        (SELECT COUNT(*) FROM chat_messages WHERE session_id = %s) AS chat_count,
                        (SELECT COUNT(*) FROM system_events WHERE session_id = %s) AS event_count
                    """,
                    (session_id, session_id),
                )
                row = cur.fetchone()
        chat_count = self._to_int(row[0] if row else 0, 0)
        event_count = self._to_int(row[1] if row else 0, 0)
        return {
            "chat_count": chat_count,
            "event_count": event_count,
            "version": chat_count + event_count,
        }

    def _read_cache_lists(
        self,
        session_id: int,
    ) -> Optional[Dict[str, Any]]:
        client = self._get_redis()
        if client is None:
            return None
        keys = self._cache_keys(session_id)
        try:
            pipe = client.pipeline()
            pipe.exists(keys["history"])
            pipe.lrange(keys["history"], 0, -1)
            pipe.exists(keys["events"])
            pipe.lrange(keys["events"], 0, -1)
            pipe.get(keys["version"])
            pipe.expire(keys["meta"], CACHE_TTL_SECONDS)
            pipe.expire(keys["history"], CACHE_TTL_SECONDS)
            pipe.expire(keys["events"], CACHE_TTL_SECONDS)
            pipe.expire(keys["version"], CACHE_TTL_SECONDS)
            (
                history_exists,
                history_raw,
                events_exists,
                events_raw,
                version_raw,
                _,
                _,
                _,
                _,
            ) = pipe.execute()
        except RedisError as exc:
            print(f"[Redis] 캐시 조회 실패: {exc}")
            return None

        if not history_exists and not events_exists:
            return None

        return {
            "history": self._deserialize_cache_entries(history_raw or []),
            "events": self._deserialize_cache_entries(events_raw or []),
            "version": self._to_int(version_raw, -1),
            "history_exists": bool(history_exists),
            "events_exists": bool(events_exists),
        }

    def _rebuild_cache_from_rows(
        self,
        session_id: int,
        chat_rows: List[Dict[str, Any]],
        event_rows: List[Dict[str, Any]],
        db_version: int,
    ) -> None:
        client = self._get_redis()
        if client is None:
            return
        keys = self._cache_keys(session_id)
        chat_entries = [
            self._serialize_chat_cache_entry(row)
            for row in chat_rows[-CACHE_HISTORY_LIMIT:]
        ]
        event_entries = [
            self._serialize_event_cache_entry(row)
            for row in event_rows[-CACHE_EVENTS_LIMIT:]
        ]
        try:
            pipe = client.pipeline()
            pipe.delete(keys["history"], keys["events"])
            if chat_entries:
                pipe.rpush(
                    keys["history"],
                    *[json.dumps(entry, ensure_ascii=False) for entry in chat_entries],
                )
            if event_entries:
                pipe.rpush(
                    keys["events"],
                    *[json.dumps(entry, ensure_ascii=False) for entry in event_entries],
                )
            pipe.set(keys["version"], str(max(0, db_version)))
            pipe.expire(keys["meta"], CACHE_TTL_SECONDS)
            pipe.expire(keys["history"], CACHE_TTL_SECONDS)
            pipe.expire(keys["events"], CACHE_TTL_SECONDS)
            pipe.expire(keys["version"], CACHE_TTL_SECONDS)
            pipe.execute()
        except RedisError as exc:
            print(f"[Redis] 캐시 재적재 실패: {exc}")

    def _refresh_meta_cache(
        self,
        session_id: int,
        current_db_id: str,
        db_list: List[str],
        sql_exec_count: int,
        last_accessed: datetime,
    ) -> None:
        client = self._get_redis()
        if client is None:
            return
        keys = self._cache_keys(session_id)
        mapping = {
            "current_db_id": current_db_id,
            "db_list": json.dumps(db_list, ensure_ascii=False),
            "sql_exec_count": str(sql_exec_count),
            "last_accessed": _to_utc_iso(last_accessed),
        }
        try:
            pipe = client.pipeline()
            pipe.hset(keys["meta"], mapping=mapping)
            pipe.expire(keys["meta"], CACHE_TTL_SECONDS)
            pipe.expire(keys["history"], CACHE_TTL_SECONDS)
            pipe.expire(keys["events"], CACHE_TTL_SECONDS)
            pipe.set(keys["version"], "0", nx=True)
            pipe.expire(keys["version"], CACHE_TTL_SECONDS)
            pipe.execute()
        except RedisError as exc:
            print(f"[Redis] 메타 캐시 갱신 실패: {exc}")

    def _append_cache_entry(
        self,
        session_id: int,
        key_name: str,
        payload: Dict[str, Any],
        max_len: int,
    ) -> None:
        client = self._get_redis()
        if client is None:
            return
        keys = self._cache_keys(session_id)
        list_key = keys[key_name]
        try:
            pipe = client.pipeline()
            pipe.rpush(list_key, json.dumps(payload, ensure_ascii=False))
            pipe.ltrim(list_key, -max_len, -1)
            pipe.expire(keys["meta"], CACHE_TTL_SECONDS)
            pipe.expire(keys["history"], CACHE_TTL_SECONDS)
            pipe.expire(keys["events"], CACHE_TTL_SECONDS)
            pipe.set(keys["version"], "0", nx=True)
            pipe.incr(keys["version"])
            pipe.expire(keys["version"], CACHE_TTL_SECONDS)
            pipe.execute()
        except RedisError as exc:
            print(f"[Redis] 캐시 append 실패 ({key_name}): {exc}")

    def ensure_session(
        self,
        user_id: str,
        default_db_id: str,
        default_db_list: List[str],
    ) -> Dict[str, Any]:
        normalized_db_list = _normalize_db_list(default_db_list, DB_LIST)
        normalized_db_id = (
            default_db_id.strip()
            if isinstance(default_db_id, str) and default_db_id.strip()
            else (normalized_db_list[0] if normalized_db_list else "")
        )
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT session_id, current_db_id, db_list, sql_exec_count
                    FROM sessions
                    WHERE user_id = %s
                    ORDER BY last_accessed DESC, session_id DESC
                    LIMIT 1
                    """,
                    (user_id,),
                )
                row = cur.fetchone()
                if row is None:
                    cur.execute(
                        """
                        INSERT INTO sessions (user_id, current_db_id, db_list, sql_exec_count)
                        VALUES (%s, %s, %s, 0)
                        RETURNING session_id, current_db_id, db_list, sql_exec_count, last_accessed
                        """,
                        (user_id, normalized_db_id, Json(normalized_db_list)),
                    )
                    session_row = cur.fetchone()
                else:
                    session_id = int(row[0])
                    current_db_id = str(row[1] or normalized_db_id)
                    db_list = _normalize_db_list(row[2], normalized_db_list)
                    sql_exec_count = int(row[3] or 0)
                    cur.execute(
                        """
                        UPDATE sessions
                        SET current_db_id = %s,
                            db_list = %s,
                            sql_exec_count = %s,
                            last_accessed = NOW()
                        WHERE session_id = %s
                        RETURNING session_id, current_db_id, db_list, sql_exec_count, last_accessed
                        """,
                        (current_db_id, Json(db_list), sql_exec_count, session_id),
                    )
                    session_row = cur.fetchone()
        if session_row is None:
            raise RuntimeError("세션 생성/조회에 실패했습니다.")
        parsed = {
            "session_id": int(session_row[0]),
            "current_db_id": str(session_row[1] or ""),
            "db_list": _normalize_db_list(session_row[2], normalized_db_list),
            "sql_exec_count": int(session_row[3] or 0),
            "last_accessed": _coerce_datetime(session_row[4]),
        }
        self._refresh_meta_cache(
            session_id=parsed["session_id"],
            current_db_id=parsed["current_db_id"],
            db_list=parsed["db_list"],
            sql_exec_count=parsed["sql_exec_count"],
            last_accessed=parsed["last_accessed"],
        )
        return parsed

    def touch_session(self, session_id: int) -> bool:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sessions
                    SET last_accessed = NOW()
                    WHERE session_id = %s
                    RETURNING current_db_id, db_list, sql_exec_count, last_accessed
                    """,
                    (session_id,),
                )
                row = cur.fetchone()
        if row is None:
            return False
        self._refresh_meta_cache(
            session_id=session_id,
            current_db_id=str(row[0] or ""),
            db_list=_normalize_db_list(row[1], DB_LIST),
            sql_exec_count=int(row[2] or 0),
            last_accessed=_coerce_datetime(row[3]),
        )
        return True

    def update_session_meta(
        self,
        session_id: int,
        current_db_id: str,
        db_list: List[str],
        sql_exec_count: int,
    ) -> Dict[str, Any]:
        normalized_db_list = _normalize_db_list(db_list, DB_LIST)
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sessions
                    SET current_db_id = %s,
                        db_list = %s,
                        sql_exec_count = %s,
                        last_accessed = NOW()
                    WHERE session_id = %s
                    RETURNING current_db_id, db_list, sql_exec_count, last_accessed
                    """,
                    (current_db_id, Json(normalized_db_list), sql_exec_count, session_id),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("세션 메타데이터 업데이트에 실패했습니다.")
        parsed = {
            "current_db_id": str(row[0] or ""),
            "db_list": _normalize_db_list(row[1], normalized_db_list),
            "sql_exec_count": int(row[2] or 0),
            "last_accessed": _coerce_datetime(row[3]),
        }
        self._refresh_meta_cache(
            session_id=session_id,
            current_db_id=parsed["current_db_id"],
            db_list=parsed["db_list"],
            sql_exec_count=parsed["sql_exec_count"],
            last_accessed=parsed["last_accessed"],
        )
        return parsed

    def increment_sql_exec_count(self, session_id: int) -> int:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE sessions
                    SET sql_exec_count = sql_exec_count + 1,
                        last_accessed = NOW()
                    WHERE session_id = %s
                    RETURNING current_db_id, db_list, sql_exec_count, last_accessed
                    """,
                    (session_id,),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("sql_exec_count 증가에 실패했습니다.")
        sql_exec_count = int(row[2] or 0)
        self._refresh_meta_cache(
            session_id=session_id,
            current_db_id=str(row[0] or ""),
            db_list=_normalize_db_list(row[1], DB_LIST),
            sql_exec_count=sql_exec_count,
            last_accessed=_coerce_datetime(row[3]),
        )
        return sql_exec_count

    def insert_chat_message(self, session_id: int, role: str, content: str) -> Dict[str, Any]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_messages (session_id, role, content)
                    VALUES (%s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (session_id, role, content),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("chat_messages 저장에 실패했습니다.")
        payload = {
            "id": int(row[0]),
            "chat_message_id": int(row[0]),
            "role": role,
            "content": content,
            "created_at": _to_utc_iso(_coerce_datetime(row[1])),
        }
        self._append_cache_entry(
            session_id=session_id,
            key_name="history",
            payload=payload,
            max_len=CACHE_HISTORY_LIMIT,
        )
        return payload

    def insert_system_event(
        self,
        session_id: int,
        event_type: str,
        content: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO system_events (session_id, event_type, content)
                    VALUES (%s, %s, %s)
                    RETURNING id, created_at
                    """,
                    (session_id, event_type, Json(content)),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("system_events 저장에 실패했습니다.")
        payload = {
            "id": int(row[0]),
            "system_event_id": int(row[0]),
            "event_type": event_type,
            "content": content,
            "created_at": _to_utc_iso(_coerce_datetime(row[1])),
        }
        self._append_cache_entry(
            session_id=session_id,
            key_name="events",
            payload=payload,
            max_len=CACHE_EVENTS_LIMIT,
        )
        return payload

    def insert_tool_call(
        self,
        session_id: int,
        tool_name: str,
        arguments: Optional[Dict[str, Any]],
        result: Optional[Dict[str, Any]],
        status: str,
        chat_message_id: Optional[int] = None,
        csv_file_path: Optional[str] = None,
    ) -> None:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO tool_calls (
                        session_id, chat_message_id, tool_name, arguments, result, status, csv_file_path
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        session_id,
                        chat_message_id,
                        tool_name,
                        Json(arguments or {}),
                        Json(result or {}),
                        status,
                        csv_file_path,
                    ),
                )

    def insert_context_note(
        self,
        session_id: int,
        note_type: str,
        topic: str,
        content: Dict[str, Any],
    ) -> Dict[str, Any]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO context_notes (session_id, note_type, topic, content)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, note_type, topic, content, created_at
                    """,
                    (session_id, note_type, topic, Json(content)),
                )
                row = cur.fetchone()
        if row is None:
            raise RuntimeError("context_notes 저장에 실패했습니다.")
        return {
            "id": int(row[0]),
            "note_type": str(row[1] or ""),
            "topic": str(row[2] or ""),
            "content": _decode_json_value(row[3]) or {},
            "created_at": _to_utc_iso(_coerce_datetime(row[4])),
        }

    def _load_chat_messages_db(self, session_id: int) -> List[Dict[str, Any]]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, role, content, created_at
                    FROM chat_messages
                    WHERE session_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(row[0]),
                "role": str(row[1] or ""),
                "content": str(row[2] or ""),
                "created_at": _coerce_datetime(row[3]),
            }
            for row in rows
        ]

    def _load_system_events_db(self, session_id: int) -> List[Dict[str, Any]]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, event_type, content, created_at
                    FROM system_events
                    WHERE session_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(row[0]),
                "event_type": str(row[1] or ""),
                "content": _decode_json_value(row[2]),
                "created_at": _coerce_datetime(row[3]),
            }
            for row in rows
        ]

    def load_chat_and_events(self, session_id: int) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        db_stats = self._compute_db_stats(session_id)
        cache_payload = self._read_cache_lists(session_id)
        if (
            cache_payload is not None
            and cache_payload.get("version") == db_stats["version"]
            and db_stats["chat_count"] <= CACHE_HISTORY_LIMIT
            and db_stats["event_count"] <= CACHE_EVENTS_LIMIT
        ):
            cached_history = cache_payload.get("history", [])
            cached_events = cache_payload.get("events", [])
            if (
                len(cached_history) == db_stats["chat_count"]
                and len(cached_events) == db_stats["event_count"]
            ):
                history_rows = [
                    {
                        "id": int(entry.get("id") or entry.get("chat_message_id") or 0),
                        "role": str(entry.get("role") or ""),
                        "content": str(entry.get("content") or ""),
                        "created_at": _coerce_datetime(entry.get("created_at")),
                    }
                    for entry in cached_history
                ]
                event_rows = [
                    {
                        "id": int(entry.get("id") or entry.get("system_event_id") or 0),
                        "event_type": str(entry.get("event_type") or ""),
                        "content": entry.get("content"),
                        "created_at": _coerce_datetime(entry.get("created_at")),
                    }
                    for entry in cached_events
                ]
                history_rows.sort(key=lambda row: (row["created_at"], row["id"]))
                event_rows.sort(key=lambda row: (row["created_at"], row["id"]))
                return history_rows, event_rows

        history_rows = self._load_chat_messages_db(session_id)
        event_rows = self._load_system_events_db(session_id)
        self._rebuild_cache_from_rows(
            session_id=session_id,
            chat_rows=history_rows,
            event_rows=event_rows,
            db_version=db_stats["version"],
        )
        return history_rows, event_rows

    def load_chat_messages(self, session_id: int) -> List[Dict[str, Any]]:
        history_rows, _ = self.load_chat_and_events(session_id)
        return history_rows

    def load_system_events(self, session_id: int) -> List[Dict[str, Any]]:
        _, event_rows = self.load_chat_and_events(session_id)
        return event_rows

    def load_context_notes(self, session_id: int) -> List[Dict[str, Any]]:
        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, note_type, topic, content, created_at
                    FROM context_notes
                    WHERE session_id = %s
                    ORDER BY created_at ASC, id ASC
                    """,
                    (session_id,),
                )
                rows = cur.fetchall()
        return [
            {
                "id": int(row[0]),
                "note_type": str(row[1] or ""),
                "topic": str(row[2] or ""),
                "content": _decode_json_value(row[3]) or {},
                "created_at": _to_utc_iso(_coerce_datetime(row[4])),
            }
            for row in rows
        ]

    def load_table_snapshot(
        self,
        session_id: int,
        table_number: int,
    ) -> Optional[Dict[str, Any]]:
        table_number_text = str(int(table_number))
        event_content: Dict[str, Any] = {}
        csv_file_path: Optional[str] = None
        tool_result: Dict[str, Any] = {}

        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT content
                    FROM system_events
                    WHERE session_id = %s
                      AND event_type = 'table_view'
                      AND content->>'table_number' = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (session_id, table_number_text),
                )
                event_row = cur.fetchone()
                if event_row is not None:
                    parsed_content = _decode_json_value(event_row[0])
                    if isinstance(parsed_content, dict):
                        event_content = parsed_content

                cur.execute(
                    """
                    SELECT csv_file_path, result
                    FROM tool_calls
                    WHERE session_id = %s
                      AND result->>'table_number' = %s
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (session_id, table_number_text),
                )
                tool_row = cur.fetchone()
                if tool_row is not None:
                    raw_csv_path = tool_row[0]
                    csv_file_path = str(raw_csv_path) if raw_csv_path else None
                    parsed_result = _decode_json_value(tool_row[1])
                    if isinstance(parsed_result, dict):
                        tool_result = parsed_result

        headers = event_content.get("headers")
        rows = event_content.get("rows")
        table_info = event_content.get("table_info")
        has_more = event_content.get("has_more")
        event_csv_path = event_content.get("csv_path")
        if not csv_file_path and isinstance(event_csv_path, str):
            csv_file_path = event_csv_path
        if not csv_file_path:
            maybe_csv_path = tool_result.get("csv_path")
            if isinstance(maybe_csv_path, str):
                csv_file_path = maybe_csv_path

        snapshot = {
            "table_number": int(table_number),
            "headers": headers if isinstance(headers, list) else [],
            "rows": rows if isinstance(rows, list) else [],
            "table_info": table_info if isinstance(table_info, str) else None,
            "has_more": _safe_bool(has_more, False),
            "csv_path": csv_file_path,
        }
        if (
            not snapshot["headers"]
            and not snapshot["rows"]
            and snapshot["csv_path"] is None
        ):
            return None
        return snapshot

    def load_timeline_page(
        self,
        session_id: int,
        before_created_at: Optional[datetime] = None,
        before_id: Optional[int] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        effective_limit = max(1, min(int(limit or 50), 200))
        is_latest_page_request = before_created_at is None and before_id is None

        def _build_items(
            chat_rows: List[Dict[str, Any]],
            event_rows: List[Dict[str, Any]],
        ) -> List[Dict[str, Any]]:
            items: List[Dict[str, Any]] = []
            for row in chat_rows:
                created_at = _coerce_datetime(row.get("created_at"))
                message_id = int(row.get("id") or 0)
                items.append(
                    {
                        "type": "chat_message",
                        "id": message_id,
                        "created_at": _to_utc_iso(created_at),
                        "role": str(row.get("role") or ""),
                        "content": str(row.get("content") or ""),
                        "_sort_created_at": created_at,
                        "_sort_id": message_id,
                    }
                )
            for row in event_rows:
                created_at = _coerce_datetime(row.get("created_at"))
                event_id = int(row.get("id") or 0)
                event_type = str(row.get("event_type") or "")
                content = row.get("content")
                item = {
                    "type": "system_event",
                    "id": event_id,
                    "created_at": _to_utc_iso(created_at),
                    "event_type": event_type,
                    "content": content,
                    "_sort_created_at": created_at,
                    "_sort_id": event_id,
                }
                history_entry = _event_to_history_entry(
                    {"event_type": event_type, "content": content}
                )
                if history_entry is not None:
                    item["history_entry"] = history_entry
                items.append(item)
            items.sort(
                key=lambda item: (item["_sort_created_at"], item["_sort_id"]),
                reverse=True,
            )
            paged_items = items[:effective_limit]
            for item in paged_items:
                item.pop("_sort_created_at", None)
                item.pop("_sort_id", None)
            return paged_items

        if is_latest_page_request:
            db_stats = self._compute_db_stats(session_id)
            cache_payload = self._read_cache_lists(session_id)
            if (
                cache_payload is not None
                and cache_payload.get("version") == db_stats["version"]
                and db_stats["chat_count"] <= CACHE_HISTORY_LIMIT
                and db_stats["event_count"] <= CACHE_EVENTS_LIMIT
                and len(cache_payload.get("history", [])) == db_stats["chat_count"]
                and len(cache_payload.get("events", [])) == db_stats["event_count"]
            ):
                cached_chat = [
                    {
                        "id": int(entry.get("id") or entry.get("chat_message_id") or 0),
                        "role": str(entry.get("role") or ""),
                        "content": str(entry.get("content") or ""),
                        "created_at": _coerce_datetime(entry.get("created_at")),
                    }
                    for entry in cache_payload.get("history", [])
                ]
                cached_events = [
                    {
                        "id": int(entry.get("id") or entry.get("system_event_id") or 0),
                        "event_type": str(entry.get("event_type") or ""),
                        "content": entry.get("content"),
                        "created_at": _coerce_datetime(entry.get("created_at")),
                    }
                    for entry in cache_payload.get("events", [])
                ]
                return _build_items(cached_chat, cached_events)

        cursor_clause = ""
        chat_params: List[Any] = [session_id]
        event_params: List[Any] = [session_id]
        if before_created_at is not None and before_id is not None:
            cursor_clause = "AND (created_at, id) < (%s, %s)"
            chat_params.extend([before_created_at, before_id])
            event_params.extend([before_created_at, before_id])

        query_limit = effective_limit
        if is_latest_page_request:
            query_limit = max(effective_limit, CACHE_HISTORY_LIMIT, CACHE_EVENTS_LIMIT)

        chat_params.append(query_limit)
        event_params.append(query_limit)

        with self._pg_connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    SELECT id, role, content, created_at
                    FROM chat_messages
                    WHERE session_id = %s
                    {cursor_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    tuple(chat_params),
                )
                chat_result_rows = cur.fetchall()
                cur.execute(
                    f"""
                    SELECT id, event_type, content, created_at
                    FROM system_events
                    WHERE session_id = %s
                    {cursor_clause}
                    ORDER BY created_at DESC, id DESC
                    LIMIT %s
                    """,
                    tuple(event_params),
                )
                event_result_rows = cur.fetchall()

        chat_rows = [
            {
                "id": int(row[0]),
                "role": str(row[1] or ""),
                "content": str(row[2] or ""),
                "created_at": _coerce_datetime(row[3]),
            }
            for row in chat_result_rows
        ]
        event_rows = [
            {
                "id": int(row[0]),
                "event_type": str(row[1] or ""),
                "content": _decode_json_value(row[2]),
                "created_at": _coerce_datetime(row[3]),
            }
            for row in event_result_rows
        ]

        if is_latest_page_request:
            db_stats = self._compute_db_stats(session_id)
            self._rebuild_cache_from_rows(
                session_id=session_id,
                chat_rows=list(reversed(chat_rows)),
                event_rows=list(reversed(event_rows)),
                db_version=db_stats["version"],
            )

        return _build_items(chat_rows, event_rows)

    def clear_cache(self, session_id: int) -> None:
        client = self._get_redis()
        if client is None:
            return
        keys = self._cache_keys(session_id)
        try:
            client.delete(keys["meta"], keys["history"], keys["events"], keys["version"])
        except RedisError as exc:
            print(f"[Redis] 세션 캐시 정리 실패: {exc}")


_STORE = _PersistentStore()


def _make_initial_history(user_id: str, session_id: Optional[int] = None) -> List[Dict[str, Any]]:
    system_prompt = CHATBOT_SYS_PROMPT.format(
        user_id=user_id,
        session_id="" if session_id is None else str(session_id),
    )
    return [{"role": "system", "content": system_prompt}]


def _event_to_history_entry(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    event_type = str(event.get("event_type") or "")
    content = event.get("content")
    if isinstance(content, dict):
        history_entry = content.get("history_entry")
        if isinstance(history_entry, dict):
            return dict(history_entry)
        if event_type == "plot" and isinstance(content.get("image"), str):
            return {"role": "assistant", "image": content["image"]}
        if event_type in {"info", "error"} and isinstance(content.get("message"), str):
            return {
                "role": "assistant",
                "type": "text",
                "content": content["message"],
            }
        if event_type == "table_view":
            entry: Dict[str, Any] = {
                "role": "assistant",
                "type": "table",
            }
            for key in ("headers", "rows", "table_info", "csv", "csv_path", "has_more"):
                if key in content:
                    entry[key] = content[key]
            if "table#" in content:
                entry["table#"] = content["table#"]
            elif "table_number" in content:
                entry["table#"] = content["table_number"]
            return entry
    return None


def _compose_evidence(notes: List[Dict[str, Any]]) -> str:
    evidence_items: List[str] = []
    for note in notes:
        evidence = note.get("evidence")
        if isinstance(evidence, str) and evidence.strip():
            evidence_items.append(evidence.strip())
    return "\n\n".join(evidence_items)


def _context_row_to_note(row: Dict[str, Any]) -> Dict[str, Any]:
    note_type = row.get("note_type", "")
    topic = str(row.get("topic") or "")
    content = row.get("content")
    if not isinstance(content, dict):
        content = {}
    if note_type == "qa":
        questions = content.get("questions")
        answers = content.get("answers")
        return {
            "type": "qa",
            "topic": topic,
            "questions": questions if isinstance(questions, list) else [],
            "answers": answers if isinstance(answers, list) else [],
            "evidence": str(content.get("evidence") or ""),
            "created_at": row.get("created_at"),
        }
    return {
        "type": "text",
        "topic": topic or "직접 입력",
        "content": str(content.get("content") or ""),
        "questions": [],
        "answers": [],
        "evidence": str(content.get("evidence") or content.get("content") or ""),
        "created_at": row.get("created_at"),
    }


def _merge_history(
    chat_messages: List[Dict[str, Any]],
    events: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    merged: List[tuple] = []
    for message in chat_messages:
        merged.append(
            (
                _coerce_datetime(message.get("created_at")),
                int(message.get("id") or 0),
                0,
                {
                    "role": message.get("role", "assistant"),
                    "content": message.get("content", ""),
                },
            )
        )
    for event in events:
        history_entry = _event_to_history_entry(event)
        if history_entry is None:
            continue
        merged.append(
            (
                _coerce_datetime(event.get("created_at")),
                int(event.get("id") or 0),
                1,
                history_entry,
            )
        )
    merged.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in merged]


def _load_session(user_id: str) -> SessionData:
    default_db_list = _normalize_db_list(DB_LIST)
    default_db_id = default_db_list[0] if default_db_list else ""
    session_meta = _STORE.ensure_session(user_id, default_db_id, default_db_list)
    session_id = int(session_meta["session_id"])
    chat_messages, events = _STORE.load_chat_and_events(session_id)
    context_rows = _STORE.load_context_notes(session_id)
    context_notes = [_context_row_to_note(row) for row in context_rows]

    history = _make_initial_history(user_id, session_id=session_id)
    history.extend(_merge_history(chat_messages, events))
    current_db_id = str(session_meta.get("current_db_id") or default_db_id)
    db_list = _normalize_db_list(session_meta.get("db_list"), default_db_list)
    if current_db_id and current_db_id not in db_list:
        db_list.append(current_db_id)

    session: SessionData = {
        "session_id": session_id,
        "chat_history": history,
        "context_notes": context_notes,
        "db_id": current_db_id,
        "db_list": db_list,
        "evidence": _compose_evidence(context_notes),
        "last_accessed": time.time(),
    }
    session["sql_exec#"] = int(session_meta.get("sql_exec_count", 0))
    session["table_results"] = {}
    return session


def _session_id(session: SessionData) -> Optional[int]:
    value = session.get("session_id")
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None


def _sync_session_meta(session: SessionData) -> None:
    session_id = _session_id(session)
    if session_id is None:
        return
    db_id = str(session.get("db_id") or "")
    db_list = _normalize_db_list(session.get("db_list"), DB_LIST)
    sql_exec_count = int(session.get("sql_exec#", 0) or 0)
    updated = _STORE.update_session_meta(
        session_id=session_id,
        current_db_id=db_id,
        db_list=db_list,
        sql_exec_count=sql_exec_count,
    )
    session["db_id"] = updated["current_db_id"]
    session["db_list"] = updated["db_list"]
    session["sql_exec#"] = updated["sql_exec_count"]


def _ensure_session(user_id: str) -> SessionData:
    session = SESSIONS.get(user_id)
    if session is None:
        session = _load_session(user_id)
        SESSIONS[user_id] = session
        _ensure_container(user_id, _session_id(session))
    session["last_accessed"] = time.time()
    if "chat_history" not in session:
        session["chat_history"] = _make_initial_history(
            user_id,
            session_id=_session_id(session),
        )
    if "context_notes" not in session:
        session["context_notes"] = []
    if "db_list" not in session:
        session["db_list"] = _normalize_db_list(DB_LIST)
    if "db_id" not in session:
        session["db_id"] = session["db_list"][0] if session["db_list"] else ""
    if "sql_exec#" not in session:
        session["sql_exec#"] = 0
    if "table_results" not in session:
        session["table_results"] = {}
    if "evidence" not in session:
        session["evidence"] = _compose_evidence(session.get("context_notes", []))
    session_id = _session_id(session)
    touched = False
    if session_id is not None:
        touched = _STORE.touch_session(session_id)
    if not touched:
        # DB 데이터가 초기화된 경우 stale session_id를 버리고 세션을 재생성한다.
        session = _load_session(user_id)
        SESSIONS[user_id] = session
        _ensure_container(user_id, _session_id(session))
    return session


def refresh_session_history(session: SessionData, user_id: str) -> List[Dict[str, Any]]:
    session_id = _session_id(session)
    if session_id is None:
        history = _make_initial_history(user_id, session_id=None)
        session["chat_history"] = history
        return history
    chat_messages, events = _STORE.load_chat_and_events(session_id)
    history = _make_initial_history(user_id, session_id=session_id)
    history.extend(_merge_history(chat_messages, events))
    session["chat_history"] = history
    return history


def refresh_session_context_notes(session: SessionData) -> List[Dict[str, Any]]:
    session_id = _session_id(session)
    if session_id is None:
        notes: List[Dict[str, Any]] = []
        session["context_notes"] = notes
        session["evidence"] = ""
        return notes
    context_rows = _STORE.load_context_notes(session_id)
    notes = [_context_row_to_note(row) for row in context_rows]
    session["context_notes"] = notes
    session["evidence"] = _compose_evidence(notes)
    return notes


def _ensure_container(user_id: str, session_id: Optional[int] = None) -> None:
    client = docker.from_env()
    container_name = f"c{user_id}"
    user_root_dir = APP_DIR / "session_data" / f"{user_id}"
    user_root_dir.mkdir(parents=True, exist_ok=True)
    if session_id is not None:
        (user_root_dir / str(session_id)).mkdir(parents=True, exist_ok=True)

    try:
        container = client.containers.get(container_name)
        container.reload()
        mounts = container.attrs.get("Mounts", []) or []
        expected_source = str(user_root_dir.resolve())
        has_expected_mount = any(
            mount.get("Destination") == "/home"
            and str(Path(mount.get("Source", "")).resolve()) == expected_source
            for mount in mounts
        )
        if has_expected_mount:
            if container.status != "running":
                container.start()
            return
        if container.status == "running":
            container.stop()
        container.remove()
    except docker.errors.NotFound:
        pass

    client.containers.run(
        PLOT_IMAGE_NAME,
        command="sleep infinity",
        detach=True,
        name=container_name,
        volumes={
            str(user_root_dir): {
                "bind": "/home",
                "mode": "rw",
            }
        },
    )


def record_chat_message(session: SessionData, role: str, content: str) -> Optional[int]:
    session_id = _session_id(session)
    if session_id is None:
        return None
    if role not in {"user", "assistant"}:
        return None
    payload = _STORE.insert_chat_message(session_id, role, content)
    return int(payload["id"])


def record_system_event(
    session: SessionData,
    event_type: str,
    history_entry: Dict[str, Any],
    content: Optional[Dict[str, Any]] = None,
) -> Optional[int]:
    session_id = _session_id(session)
    if session_id is None:
        return None
    payload: Dict[str, Any] = {"history_entry": history_entry}
    if isinstance(content, dict):
        payload.update(content)
    stored = _STORE.insert_system_event(session_id, event_type, payload)
    return int(stored["id"])


def record_tool_call(
    session: SessionData,
    tool_name: str,
    arguments: Optional[Dict[str, Any]],
    result: Optional[Dict[str, Any]],
    status: str = "success",
    chat_message_id: Optional[int] = None,
    csv_file_path: Optional[str] = None,
) -> None:
    session_id = _session_id(session)
    if session_id is None:
        return
    _STORE.insert_tool_call(
        session_id=session_id,
        tool_name=tool_name,
        arguments=arguments,
        result=result,
        status=status,
        chat_message_id=chat_message_id,
        csv_file_path=csv_file_path,
    )


def increment_sql_exec_count(session: SessionData) -> int:
    session_id = _session_id(session)
    if session_id is None:
        session["sql_exec#"] = int(session.get("sql_exec#", 0) or 0) + 1
        return int(session["sql_exec#"])
    new_value = _STORE.increment_sql_exec_count(session_id)
    session["sql_exec#"] = new_value
    return new_value


def set_current_db(session: SessionData, db_id: str) -> None:
    session["db_id"] = db_id
    _sync_session_meta(session)


def add_database(session: SessionData, db_id: str, set_current: bool = False) -> None:
    db_list = _normalize_db_list(session.get("db_list"), DB_LIST)
    if db_id not in db_list:
        db_list.append(db_id)
    session["db_list"] = db_list
    if set_current:
        session["db_id"] = db_id
    _sync_session_meta(session)


def save_context_note(
    session: SessionData,
    note_type: str,
    topic: str,
    content: Dict[str, Any],
) -> Dict[str, Any]:
    session_id = _session_id(session)
    if session_id is None:
        raise RuntimeError("세션 식별자가 없습니다.")
    row = _STORE.insert_context_note(
        session_id=session_id,
        note_type=note_type,
        topic=topic,
        content=content,
    )
    note = _context_row_to_note(row)
    notes = session.setdefault("context_notes", [])
    notes.append(note)
    session["evidence"] = _compose_evidence(notes)
    return note


def fetch_timeline_page(
    session_id: int,
    before_created_at: Optional[datetime] = None,
    before_id: Optional[int] = None,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    return _STORE.load_timeline_page(
        session_id=session_id,
        before_created_at=before_created_at,
        before_id=before_id,
        limit=limit,
    )


def fetch_table_snapshot(
    session: SessionData,
    table_number: int,
) -> Optional[Dict[str, Any]]:
    session_id = _session_id(session)
    if session_id is None:
        return None
    return _STORE.load_table_snapshot(session_id, table_number)


def _remove_container(client: docker.DockerClient, user_id: str) -> None:
    container_name = f"c{user_id}"
    try:
        container = client.containers.get(container_name)
        if container.status == "running":
            container.stop()
        container.remove()
    except docker.errors.NotFound:
        return
    except Exception as exc:
        print(f"[Cleanup] Error removing container {container_name}: {exc}")


def cleanup_inactive_sessions():
    """
    마지막 접근 시간이 SESSION_TIMEOUT_SECONDS(1시간) 이상 지난 세션을 정리합니다.
    SESSIONS 딕셔너리에서 제거하고, Docker 컨테이너도 중지 및 삭제합니다.
    """
    now = time.time()
    inactive_users: List[str] = []

    for user_id, session in list(SESSIONS.items()):
        last_accessed = float(session.get("last_accessed", 0) or 0)
        if now - last_accessed > SESSION_TIMEOUT_SECONDS:
            inactive_users.append(user_id)

    if not inactive_users:
        return

    client = docker.from_env()
    for user_id in inactive_users:
        print(f"[Cleanup] Removing inactive session for user: {user_id}")
        session = SESSIONS.pop(user_id, None)
        if session:
            session_id = _session_id(session)
            if session_id is not None:
                _STORE.clear_cache(session_id)
        _remove_container(client, user_id)
