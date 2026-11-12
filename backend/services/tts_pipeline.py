#region import
from __future__ import annotations


import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple, TypedDict, Union

from tabulate import tabulate

from openai import OpenAI
from langchain_core.documents import Document
from langchain_core.output_parsers import JsonOutputParser, StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field
from langchain_core.runnables import RunnableLambda
from langchain_openai import ChatOpenAI
try:
    from langchain_core.caches import BaseCache
    from langchain_core.callbacks import Callbacks
    ChatOpenAI.model_rebuild()
except Exception:
    pass
from langgraph.graph import END, START, StateGraph
from langchain_community.retrievers import BM25Retriever
from langchain.retrievers import EnsembleRetriever
from langchain_community.vectorstores import FAISS
from langchain_openai import OpenAIEmbeddings

from .qdrant_retriever import QdrantSchemaRetriever
from ..config import (
    APP_DIR,
    BASE_DIR,
    ORACLE_JSON_PATH,
    PROMPT_DIR,
    RERANK_URL,
)
#endregion
#API key 입력 혹은 환경변수로 설정해주셔야 합니다
# Configure OPENAI_API_KEY in the process environment.

with open(ORACLE_JSON_PATH, 'r', encoding='utf-8') as file:
    ORACLE_JSON = json.load(file)

#region retriever
import requests

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

client = OpenAI(base_url=os.environ.get("EMBEDDING_BASE_URL", "http://localhost:8001/v1"), api_key=os.environ.get("EMBEDDING_API_KEY") or "EMPTY")

def replace_bracket_value(s: str, new_value: str) -> str:
    before, rest = s.split("[", 1)
    _, after = rest.split("]", 1)
    return f"{before}[{new_value[1:-1]}]{after}"

def rerank(query:str, columns:List[str], context):
    payload = {
        "query": query,
        "instruction": instruction.format(context=context),
        "documents": columns
    }
    resp = requests.post(RERANK_URL, json=payload)
    resp.raise_for_status()
    return (resp.json())["results"]

def embed(input: str):
    response = client.embeddings.create(
        model = "/workspace/models/Qwen3-Embedding-0.6B",
        input= input
    )
    return response.data[0].embedding

OPENAI_CLIENT = OpenAI()
RESPONSES_MODEL = os.environ.get("CHATBOT_RESPONSES_MODEL", "gpt-5.2")

_GRAPH_RETRIEVERS: Dict[str, QdrantSchemaRetriever] = {}

def _get_graph_retriever(db_id: Optional[str]) -> QdrantSchemaRetriever:
    key = db_id or ""
    retriever = _GRAPH_RETRIEVERS.get(key)
    if retriever is None:
        retriever = QdrantSchemaRetriever(db_id=db_id)
        _GRAPH_RETRIEVERS[key] = retriever
    return retriever

#--- RAG 벡터 스페이스 load ------------------------------------------------------------
persist_dir = Path(os.environ.get("FAISS_INDEX_PATH") or (APP_DIR / "vectorstores" / "faiss_store"))

embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
vectorstore = FAISS.load_local(
    str(persist_dir),
    embeddings,
    allow_dangerous_deserialization=True,
)

docs: list[Document] = vectorstore.docstore._dict.values()
bm25_retriever = BM25Retriever.from_documents(docs)
embeddings = OpenAIEmbeddings(model="text-embedding-3-small")
faiss_retriever = vectorstore.as_retriever()
ensemble_retriever = EnsembleRetriever(
    retrievers=[bm25_retriever, faiss_retriever],
    weights=[0.5, 0.5]
)

PLOT_IMAGE_NAME = "plot"
PLOT_TMP_DIR = "/home"
SQL_EXEC_TRACK_URL = os.environ.get("SQL_EXEC_TRACK_URL", "http://127.0.0.1:1234/ui/sql_exec")
# --- 프롬프트 로더 --------------------------------------------------------------------
def load_prompt(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Prompt file not found: {path}")
    return path.read_text(encoding="utf-8")

DECOMPOSER_SYS_PROMPT = load_prompt(PROMPT_DIR / "DECOMPOSER_SYS_PROMPT.txt")
DECOMPOSER_NEW_SYS_PROMPT = load_prompt(PROMPT_DIR / "SQLITE_DECOMPOSER_SYS_PROMPT.txt")
# DECOMPOSER_SYS_PROMPT = load_prompt(PROMPT_DIR / "prompt.txt")
RETRIEVAL_TEMPLATE = load_prompt(PROMPT_DIR / "RETRIEVAL_FORMAT.txt")
CHATBOT_SYS_PROMPT = load_prompt(PROMPT_DIR / "SYS_PROMPT-mod.txt")
PROMODE_SYS_PROMPT = load_prompt(PROMPT_DIR / "PROMODE_SYS_PROMPT.txt")
COLMAKER_SYS_PROMPT = load_prompt(PROMPT_DIR / "COLMAKER_SYS_PROMPT.txt")
# --- 유틸리티 함수 ---------------------------------------------------------------------
def format_docs(docs: List[Document]) -> str:
    """
    Document 객체 리스트의 page_content를 
    하나의 문자열로 합칩니다.
    """
    return "\n\n".join(doc.page_content for doc in docs)

def extract_sql(text: str) -> Optional[str]:
    end_pos = text.rfind("```")
    if end_pos == -1:
        return None
    start_pos = text.rfind("```", 0, end_pos)
    if start_pos == -1:
        return None
    inner_content = text[start_pos + 3 : end_pos]
    first_newline_pos = inner_content.find("\n")
    if first_newline_pos == -1:
        return ""
    content_after_start_line = inner_content[first_newline_pos + 1 :]
    last_newline_pos = content_after_start_line.rfind("\n")
    if last_newline_pos == -1:
        sql = content_after_start_line
    else:
        sql = content_after_start_line[:last_newline_pos]
    return sql.replace(";", "")
# --- LLM 세팅 -------------------------------------------------------------------------
#local_llm = ChatOpenAI(model="/workspace/models/gpt-oss-120b", base_url = "http://127.0.0.1:8000/v1", openai_api_key="dummy", reasoning = {"effort": "medium"})
local_llm = ChatOpenAI(model="gpt-5.2", temperature=0.0, reasoning={"effort": "medium"})
mini_llm = ChatOpenAI(model="gpt-5-mini", reasoning={"effort": "low"})
class PromodeOutput(BaseModel):
    is_enough: bool = Field(...)
    llm_question: List[str] = Field(...)

class BatchTranslation(BaseModel):
    translated_text: List[str] = Field(...)

retriever_chain = ensemble_retriever | RunnableLambda(format_docs)

decomposer_prompt = ChatPromptTemplate.from_template(DECOMPOSER_SYS_PROMPT)
decomposer_chain = decomposer_prompt | local_llm | StrOutputParser()

promode_prompt = ChatPromptTemplate.from_template(PROMODE_SYS_PROMPT)
promode_parser = JsonOutputParser(pydantic_object=PromodeOutput)
promode_chain = promode_prompt | local_llm | promode_parser

colmaker_prompt = ChatPromptTemplate.from_template(COLMAKER_SYS_PROMPT)
colmaker_parser = JsonOutputParser()
colmaker_chain = colmaker_prompt | mini_llm | colmaker_parser

decomposer_for_new_prompt = ChatPromptTemplate.from_template(DECOMPOSER_NEW_SYS_PROMPT)
decomposer_chain_for_new = decomposer_for_new_prompt | local_llm | StrOutputParser()

# --- 상태 정의 ------------------------------------------------------------------------
class AnalysisState(TypedDict, total=False):
    user_prompt: str
    db_id: str
    desc_str: str
    fk_str: str
    evidence: str
    formal_question: str
    context: str
    promode_questions: List[str]

class DirectState(TypedDict, total=False):
    user_prompt: str
    db_id: str
    desc_str: str
    fk_str: str
    evidence: str
    selector_output: Dict[str, Union[str, List[str]]]
    processed_desc_str: str
    final_output: str
    context: str

def promode_node(state: AnalysisState) -> AnalysisState:
    start = time.time()
    result = promode_chain.invoke(
        {
            "desc_str": state.get("desc_str", ""),
            "query": state["user_prompt"],
            "evidence": "",
            "context": ""
        }
    )
    elapsed = time.time() -start
    print(f"Promode took {elapsed:.4f} seconds")
    state["promode_questions"] = result.get("llm_question", [])
    return state

def direct_retrieval_node(state: DirectState) -> DirectState:
    start = time.time()
    query = state.get("user_prompt", "")
    context = retriever_chain.invoke(query)
    elapsed = time.time() - start
    print(f"Context retrieval took {elapsed:.4f} seconds; length={len(context)}")
    print(context)
    state["context"] = context
    return state

def decomposer_node_direct(state: DirectState) -> DirectState:
    start = time.time()
    final_output = decomposer_chain.invoke(
        {
            "db_id": state["db_id"],
            "query": state.get("formal_question", state["user_prompt"]),
            "evidence": state.get("evidence", ""),
            "desc_str": state["desc_str"],
            "fk_str": state["fk_str"],
            "context": state["context"]
        }
    )
    #print(state.get("processed_desc_str"))
    elapsed = time.time() - start
    #print(final_output)
    print(f"Decomposer took {elapsed:.4f} seconds")
    state["final_output"] = final_output
    return state

def decomposer_node_direct_for_new(state: DirectState) -> DirectState:
    start = time.time()
    final_output = decomposer_chain_for_new.invoke(
        {
            "db_id": state["db_id"],
            "query": state.get("formal_question", state["user_prompt"]),
            "evidence": state.get("evidence", ""),
            "desc_str": state["desc_str"],
            "fk_str": state["fk_str"],
            "context": state["context"]
        }
    )
    #print(state.get("processed_desc_str"))
    elapsed = time.time() - start
    #print(final_output)
    print(f"Decomposer took {elapsed:.4f} seconds")
    state["final_output"] = final_output
    return state
# --- 그래프 구성 ----------------------------------------------------------------------
def build_direct_graph() -> StateGraph:
    graph: StateGraph = StateGraph(DirectState)
    graph.add_node("decomposer", decomposer_node_direct)
    graph.add_node("retrieval", direct_retrieval_node)

    graph.add_edge(START, "decomposer")
    graph.add_edge("decomposer", END)
    return graph

def build_direct_graph_for_new() -> StateGraph:
    graph: StateGraph = StateGraph(DirectState)
    graph.add_node("decomposer", decomposer_node_direct_for_new)
    graph.add_node("retrieval", direct_retrieval_node)

    graph.add_edge(START, "decomposer")
    graph.add_edge("decomposer", END)
    return graph

def build_retreival_graph() -> StateGraph:
    graph: StateGraph = StateGraph(DirectState)
    graph.add_node("retrieval", direct_retrieval_node)

    graph.add_edge(START, "retrieval")
    graph.add_edge("retrieval", END)
    return graph

def build_promode_graph() -> StateGraph:
    graph: StateGraph = StateGraph(AnalysisState)
    graph.add_node("promode", promode_node)

    graph.add_edge(START, "promode")
    graph.add_edge("promode", END)
    return graph

DIRECT_GRAPH = build_direct_graph().compile()
PROMODE_GRAPH = build_promode_graph().compile()
RETRIEVAL_GRAPH = build_retreival_graph().compile()
DIRECT_GRAPH_NEW = build_direct_graph_for_new().compile()
# --- 공개 API -------------------------------------------------------------------------
class DirectResult(TypedDict, total=False):
    is_relevant: bool
    formal_question: str
    selector_output: Dict[str, Union[str, List[str]]]
    processed_desc_str: str
    final_output: str

def run_promode(
    question: str,
    db_id: str
) -> List[str]:
    import ipdb; ipdb.set_trace()
    retriever = _get_graph_retriever(db_id)
    desc_str = retriever.get_relevant_schema(question)
    print(desc_str)
    fk_str = ""
    state: AnalysisState = {
        "user_prompt": question,
        "db_id": db_id,
        "desc_str": desc_str,
        "fk_str": fk_str
    }
    result = PROMODE_GRAPH.invoke(state)
    return result.get("promode_questions", [])

def run_direct(
    question: str,
    evidence: str,
    db_id: str
) -> str:
    #DirectResult:
    state: DirectState = {
        "user_prompt": question
    }
    result = RETRIEVAL_GRAPH.invoke(state)
    retriever = _get_graph_retriever(db_id)
    desc_str = retriever.get_relevant_schema(question, result["context"])
    print(desc_str)
    fk_str = ""
    state: DirectState = {
        "user_prompt": question,
        "db_id": db_id,
        "desc_str": desc_str,
        "fk_str": fk_str,
        "evidence": evidence,
        "context": result["context"]
    }
    result = DIRECT_GRAPH.invoke(state)
    return extract_sql(result.get("final_output"))

def run_direct_for_new(
    question: str,
    evidence: str,
    user_id: str,
    db_id: str
) -> str:
    #DirectResult:
    state: DirectState = {
        "user_prompt": question
    }
    result = RETRIEVAL_GRAPH.invoke(state)
    desc_str_path = APP_DIR / "uploads" / user_id / db_id / f"{db_id}_description.txt"
    with open(desc_str_path, "r", encoding="utf-8") as f:
        desc_str = f.read()
    fk_str = ""
    state: DirectState = {
        "user_prompt": question,
        "db_id": db_id,
        "desc_str": desc_str,
        "fk_str": fk_str,
        "evidence": evidence,
        "context": result["context"]
    }
    result = DIRECT_GRAPH_NEW.invoke(state)
    return extract_sql(result.get("final_output"))

def run_colmaker(
    llm_input_list: List[str]
) -> List[str]:
    result = colmaker_chain.invoke(
        {
            "input": llm_input_list
        }
    )
    return result
