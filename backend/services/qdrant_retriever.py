from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from openai import OpenAI
from qdrant_client import QdrantClient

from ..config import APP_DIR, RERANK_URL


OPENAI_BASE_URL = "http://127.0.0.1:8001/v1"
EMBED_MODEL = "/workspace/models/Qwen3-Embedding-0.6B"

QDRANT_HOST = os.environ.get("QDRANT_HOST", "localhost")
QDRANT_PORT = int(os.environ.get("QDRANT_PORT", "6333"))

COLUMN_COLLECTION_NAME = "column_collection"
TABLE_COLLECTION_NAME = "table_collection"

DEFAULT_METADATA_DB_PATH = APP_DIR / "metadata" / "metadata.sqlite"
ENABLE_RERANK = os.environ.get("ENABLE_RERANK", "false").strip().lower() in {
    "1",
    "true",
    "yes",
    "y",
    "on",
}
RERANK_TOP_K = int(os.environ.get("RERANK_TOP_K", "7"))

instruction = """
You are selecting database columns that are likely required to write a correct SQL query for the user question.

Return "yes" ONLY if the column is directly needed for:
1) filtering conditions (WHERE),
2) grouping/aggregation (GROUP BY / HAVING),
3) joins/keys (JOIN),
4) selected outputs (SELECT),
5) sorting (ORDER BY)

Return "no" if the column is merely background information, redundant, or only loosely related.
Be conservative: prefer "no" unless the column is clearly useful for producing SQL.
Take care of [Context].
[Context]
{context}
"""

openai_client = OpenAI(base_url=OPENAI_BASE_URL, api_key="dummy")


def embed(text: str) -> List[float]:
    response = openai_client.embeddings.create(model=EMBED_MODEL, input=text)
    return response.data[0].embedding


def rerank(query: str, columns: List[str], context: str) -> List[Dict[str, Any]]:
    payload = {
        "query": query,
        "instruction": instruction.format(context=context),
        "documents": columns,
    }
    resp = requests.post(RERANK_URL, json=payload, timeout=8)
    resp.raise_for_status()
    body = resp.json()
    results = body.get("results", [])
    return results if isinstance(results, list) else []


class QdrantSchemaRetriever:
    def __init__(
        self,
        db_id: Optional[str] = None,
        metadata_db_path: Path = DEFAULT_METADATA_DB_PATH,
    ) -> None:
        self.db_id = db_id
        self.metadata_db_path = Path(metadata_db_path)
        self.client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
        self.W_TABLE = 0.5
        self.W_COL_MAX = 0.35
        self.W_COL_CNT = 0.15

    def get_relevant_schema(self, question: str, context: str = "") -> str:
        if not self.metadata_db_path.exists():
            raise FileNotFoundError(f"metadata.sqlite not found: {self.metadata_db_path}")

        query_vector = embed(question)
        column_points = self.client.query_points(
            collection_name=COLUMN_COLLECTION_NAME,
            query=query_vector,
            limit=30,
        ).points
        table_points = self.client.query_points(
            collection_name=TABLE_COLLECTION_NAME,
            query=query_vector,
            limit=5,
        ).points

        column_rows = self._filter_rows(self._points_to_rows(column_points))
        table_rows = self._filter_rows(self._points_to_rows(table_points))

        ranked_tables = self._calculate_hybrid_scores(column_rows, table_rows)
        top_tables = [entry["table_name"] for entry in ranked_tables[:2]]
        if not top_tables:
            return ""
        with sqlite3.connect(self.metadata_db_path) as conn:
            return self._build_desc_str(conn, top_tables, question, context)

    def _filter_rows(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not self.db_id:
            return rows
        filtered = [
            row for row in rows if row.get("payload", {}).get("db_id") == self.db_id
        ]
        return filtered

    @staticmethod
    def _points_to_rows(points: List[Any]) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for point in points:
            rows.append(
                {
                    "id": point.id,
                    "payload": point.payload or {},
                    "score": point.score,
                }
            )
        return rows

    def _calculate_hybrid_scores(
        self,
        column_rows: List[Dict[str, Any]],
        table_rows: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        scores: Dict[str, Dict[str, Any]] = {}
        for row in column_rows:
            table_name = row.get("payload", {}).get("table_name")
            if not table_name:
                continue
            entry = scores.setdefault(
                table_name,
                {"table_name": table_name, "highest_column_score": 0.0, "table_score": 0.0, "column_cnt": 0},
            )
            entry["column_cnt"] += 1
            if row["score"] > entry["highest_column_score"]:
                entry["highest_column_score"] = row["score"]

        for row in table_rows:
            table_name = row.get("payload", {}).get("table_name")
            if not table_name:
                continue
            entry = scores.setdefault(
                table_name,
                {"table_name": table_name, "highest_column_score": 0.0, "table_score": 0.0, "column_cnt": 0},
            )
            entry["table_score"] = row["score"]

        for entry in scores.values():
            norm_cnt = min(entry["column_cnt"], 5) / 5.0
            entry["final_score"] = (
                self.W_TABLE * entry["table_score"]
                + self.W_COL_MAX * entry["highest_column_score"]
                + self.W_COL_CNT * norm_cnt
            )

        return sorted(scores.values(), key=lambda x: x["final_score"], reverse=True)

    @staticmethod
    def _build_desc_str(
        conn: sqlite3.Connection,
        table_names: List[str],
        query: str,
        context: str,
    ) -> str:
        desc_parts: List[str] = []
        for table_name in table_names:
            row = conn.execute(
                "SELECT table_desc FROM table_metadata WHERE table_name = ?;",
                (table_name,),
            ).fetchone()
            if row is None:
                continue
            table_desc = row[0]
            desc_parts.append(f"#Table: {table_name}\n{table_desc}\nColumns\n")
            desc_parts.append("[\n")
            col_rows = conn.execute(
                "SELECT column_desc FROM column_metadata WHERE table_name = ? ORDER BY column_name;",
                (table_name,),
            ).fetchall()
            col_desc_list = [column_desc for (column_desc,) in col_rows]
            if col_desc_list:
                selected_columns = col_desc_list
                if ENABLE_RERANK:
                    try:
                        ranked = rerank(query, col_desc_list, context)
                        reranked_documents = [
                            str(item.get("document", "")).strip()
                            for item in ranked
                            if isinstance(item, dict)
                        ]
                        reranked_documents = [doc for doc in reranked_documents if doc]
                        if reranked_documents:
                            limit = max(1, min(RERANK_TOP_K, len(reranked_documents)))
                            selected_columns = reranked_documents[:limit]
                    except Exception as exc:
                        print(
                            f"[RERANK] failed, fallback to full column list for table '{table_name}': {exc}"
                        )

                for column_desc in selected_columns:
                    if not column_desc:
                        continue
                    desc_parts.append(f" {column_desc}\n")
            desc_parts.append("]\n")
        return "".join(desc_parts)
