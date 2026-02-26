import { useEffect, useMemo, useRef, useState } from "react";
import MessageList from "./components/MessageList.jsx";
import ChatInput from "./components/ChatInput.jsx";

const DEFAULT_STATUS = "로그인 사용자 정보를 확인하는 중입니다.";
const SQL_LOADING_MESSAGE = "SQL을 생성 중입니다...";
const BUG_REPORT_MAX_LENGTH = 2000;
const TTS_API_BASE = "/api/tts";
const ttsApi = (path) => `${TTS_API_BASE}${path}`;
const isInlineImage = (value) =>
  typeof value === "string" && value.startsWith("data:image/");

const parseSqlFormattedResponse = (raw) => {
  if (typeof raw !== "string") {
    return null;
  }
  const trimmed = raw.trim();
  if (!trimmed.toLowerCase().startsWith("**sql**")) {
    return null;
  }
  const codeBlockRegex = /```[ \t]*sql[ \t]*\n([\s\S]*?)```/i;
  const codeMatch = trimmed.match(codeBlockRegex);
  if (!codeMatch) {
    return null;
  }
  const code = codeMatch[1].trim();
  const before = trimmed.slice(0, codeMatch.index);
  const after = trimmed.slice(codeMatch.index + codeMatch[0].length);
  let remainder = `${before}${after}`.replace(/\*\*sql\*\*/gi, "").trim();
  remainder = remainder.replace(/\n{3,}/g, "\n\n");
  return {
    code,
    description: remainder,
  };
};

const mapHistoryItems = (items = []) =>
  items
    .flatMap((item) => {
      const role = item?.role === "assistant" ? "assistant" : "user";
      const declaredType =
        typeof item?.type === "string" ? item.type.toLowerCase() : "";
      if (declaredType === "table") {
        const headers = Array.isArray(item.headers)
          ? item.headers.map((header) => String(header))
          : [];
        const rows = Array.isArray(item.rows)
          ? item.rows.map((row) =>
            Array.isArray(row)
              ? row.map((cell) => (cell == null ? "" : String(cell)))
              : [],
          )
          : [];
        const rawTableNumber = item?.["table#"] ?? item?.table_number;
        const tableNumber =
          typeof rawTableNumber === "number"
            ? rawTableNumber
            : typeof rawTableNumber === "string" && rawTableNumber.trim()
              ? Number(rawTableNumber)
              : null;
        const hasMore =
          typeof item?.has_more === "boolean"
            ? item.has_more
            : typeof item?.hasMore === "boolean"
              ? item.hasMore
              : false;
        if (headers.length === 0 && rows.length === 0) {
          return [];
        }
        return {
          role,
          type: "table",
          headers,
          rows,
          tableInfo: typeof item.table_info === "string" ? item.table_info : null,
          csv: typeof item.csv === "string" ? item.csv : null,
          csvPath: typeof item.csv_path === "string" ? item.csv_path : null,
          tableNumber: Number.isFinite(tableNumber) ? tableNumber : null,
          hasMore,
        };
      }

      const imagePayload = item?.image || item?.img;
      if (isInlineImage(imagePayload)) {
        return { role, type: "image", src: imagePayload, alt: "생성된 이미지" };
      }
      const content = item?.content ?? "";
      if (role === "assistant" && typeof content === "string") {
        const parsed = parseSqlFormattedResponse(content);
        if (parsed) {
          const nextItems = [
            {
              role,
              type: "code",
              code: parsed.code,
              language: "sql",
            },
          ];
          if (parsed.description) {
            nextItems.push({
              role,
              type: "text",
              content: parsed.description,
            });
          }
          return nextItems;
        }
      }
      return {
        role,
        type: "text",
        content,
      };
    })
    .filter(Boolean);

const getLastAssistantSql = (history = []) => {
  let sqlSnippet = "";
  let seenAssistant = false;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const item = history[index];
    if (item.role === "assistant") {
      seenAssistant = true;
      if (item.type === "code" && (item.language || "").toLowerCase() === "sql") {
        sqlSnippet = item.code || "";
        break;
      }
    } else if (seenAssistant && item.role === "user") {
      break;
    }
  }
  return sqlSnippet;
};

const stripSqlLoadingMessages = (items = []) =>
  items.filter(
    (item) =>
      !(
        item?.role === "assistant" &&
        item?.type === "loading" &&
        item?.content === SQL_LOADING_MESSAGE
      ),
  );



const resolveJson = async (url, options = {}) => {
  const response = await fetch(url, {
    credentials: "include",
    ...options,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("로그인이 필요합니다.");
  }
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = payload?.detail || "";
    } catch (_error) {
      // JSON 파싱 실패는 사용자에게 노출하지 않음
    }
    throw new Error(detail || "요청이 실패했습니다.");
  }
  return response.json();
};

const formatSessionTimestamp = (value) => {
  if (!value) {
    return "";
  }
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return "";
  }
  const now = new Date();
  const isSameDay =
    parsed.getFullYear() === now.getFullYear() &&
    parsed.getMonth() === now.getMonth() &&
    parsed.getDate() === now.getDate();
  if (isSameDay) {
    return parsed.toLocaleTimeString("ko-KR", {
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return parsed.toLocaleDateString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
  });
};

const getProfileInitial = (displayName, userId) => {
  const source = String(displayName || userId || "").trim();
  if (!source) {
    return "?";
  }
  return source.slice(0, 1).toUpperCase();
};

export default function App() {
  const [themeMode, setThemeMode] = useState(() => {
    try {
      const stored = window.localStorage.getItem("tts-theme-mode");
      return stored === "light" ? "light" : "dark";
    } catch (_error) {
      return "dark";
    }
  });
  const [currentUserId, setCurrentUserId] = useState("");
  const [currentUsername, setCurrentUsername] = useState("");
  const [statusMessage, setStatusMessage] = useState(DEFAULT_STATUS);
  const [authError, setAuthError] = useState("");
  const [chatHistory, setChatHistory] = useState([]);

  const [tableMoreLoading, setTableMoreLoading] = useState({});
  const [loading, setLoading] = useState(false);
  const [sqlMode, setSqlMode] = useState(false);
  const [sqlRunning, setSqlRunning] = useState(false);
  const [sqlTesting, setSqlTesting] = useState(false);
  const [lastAssistantSql, setLastAssistantSql] = useState("");
  const [showSqlButton, setShowSqlButton] = useState(false);
  const [currentDbId, setCurrentDbId] = useState("");
  const [sessionsSidebarOpen, setSessionsSidebarOpen] = useState(false);
  const [sessionsLoading, setSessionsLoading] = useState(false);
  const [sessionActionLoading, setSessionActionLoading] = useState(false);
  const [sessionError, setSessionError] = useState("");
  const [sessionList, setSessionList] = useState([]);
  const [currentSessionId, setCurrentSessionId] = useState(null);
  const [profileMenuOpen, setProfileMenuOpen] = useState(false);
  const [profileActionLoading, setProfileActionLoading] = useState(false);
  const [helpSubmenuOpen, setHelpSubmenuOpen] = useState(false);
  const [bugReportModalOpen, setBugReportModalOpen] = useState(false);
  const [bugReportDescription, setBugReportDescription] = useState("");
  const [bugReportSubmitting, setBugReportSubmitting] = useState(false);
  const [bugReportError, setBugReportError] = useState("");
  const [bugReportSuccess, setBugReportSuccess] = useState("");
  const [systemToastMessage, setSystemToastMessage] = useState("");
  const profileMenuRef = useRef(null);
  const helpSubmenuCloseTimerRef = useRef(null);

  const applySessionHistory = (historyPayload) => {
    const history = Array.isArray(historyPayload)
      ? mapHistoryItems(historyPayload)
      : [];
    setChatHistory(history);
    const sqlSnippet = getLastAssistantSql(history);
    setLastAssistantSql(sqlSnippet);
    setShowSqlButton(Boolean(sqlSnippet));
    setTableMoreLoading({});
    return history;
  };

  const loadHistory = async () => {
    setChatHistory([
      {
        role: "assistant",
        type: "text",
        content: "이전 대화를 불러오는 중...",
      },
    ]);
    setTableMoreLoading({});

    try {
      const data = await resolveJson(ttsApi("/history"));
      applySessionHistory(data.history);
    } catch (error) {
      setChatHistory([
        {
          role: "assistant",
          type: "text",
          content: error.message || "대화 기록을 불러오는 중 오류가 발생했습니다.",
        },
      ]);
      setLastAssistantSql("");
      setSqlMode(false);
      setSqlRunning(false);
      setSqlTesting(false);
      setShowSqlButton(false);
      setTableMoreLoading({});
    }
  };

  const fetchSessionList = async (userIdOverride = "") => {
    const effectiveUserId = (userIdOverride || currentUserId || "").trim();
    if (!effectiveUserId) {
      return [];
    }
    setSessionsLoading(true);
    setSessionError("");
    try {
      const data = await resolveJson(ttsApi("/sessions"));
      const normalized = Array.isArray(data.sessions)
        ? data.sessions
          .map((item) => {
            if (!item || typeof item !== "object") {
              return null;
            }
            const sessionId =
              typeof item.session_id === "number"
                ? item.session_id
                : Number(item.session_id);
            if (!Number.isFinite(sessionId) || sessionId <= 0) {
              return null;
            }
            return {
              session_id: sessionId,
              title:
                typeof item.title === "string" && item.title.trim()
                  ? item.title.trim()
                  : `새 대화 ${sessionId}`,
              preview:
                typeof item.preview === "string" ? item.preview.trim() : "",
              message_count:
                typeof item.message_count === "number"
                  ? item.message_count
                  : Number(item.message_count) || 0,
              created_at:
                typeof item.created_at === "string" ? item.created_at : "",
              last_accessed:
                typeof item.last_accessed === "string" ? item.last_accessed : "",
            };
          })
          .filter(Boolean)
        : [];
      setSessionList(normalized);
      const rawCurrent = data.current_session_id;
      const parsedCurrent =
        typeof rawCurrent === "number" ? rawCurrent : Number(rawCurrent);
      setCurrentSessionId(Number.isFinite(parsedCurrent) ? parsedCurrent : null);
      return normalized;
    } catch (error) {
      setSessionError(error.message || "세션 목록을 불러오는 중 오류가 발생했습니다.");
      return [];
    } finally {
      setSessionsLoading(false);
    }
  };

  const loadUserDb = async () => {
    try {
      const data = await resolveJson(ttsApi("/db"));
      const dbId = typeof data.db_id === "string" ? data.db_id.trim() : "";
      setCurrentDbId(dbId);
      return dbId;
    } catch (_error) {
      setCurrentDbId("");
      return "";
    }
  };

  const fetchDbList = async () => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const data = await resolveJson(ttsApi("/db/list"));
    const normalized = Array.isArray(data.databases)
      ? data.databases
        .map((item) => (typeof item === "string" ? item.trim() : ""))
        .filter(Boolean)
      : [];

    return normalized;
  };

  const changeDb = async (dbId) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const trimmed = (dbId || "").trim();
    if (!trimmed) {
      throw new Error("변경할 DB를 선택해 주세요.");
    }
    await resolveJson(ttsApi("/db/select"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        db_id: trimmed,
      }),
    });
    setCurrentDbId(trimmed);
    return trimmed;
  };

  const uploadDbCsv = async ({ file, dbId }) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    if (!file) {
      throw new Error("CSV 파일을 선택해 주세요.");
    }
    const formData = new FormData();
    formData.append("file", file);
    if (dbId && typeof dbId === "string" && dbId.trim()) {
      formData.append("db_id", dbId.trim());
    }
    return resolveJson(ttsApi("/db/upload"), {
      method: "POST",
      body: formData,
    });
  };

  const convertDbCsv = async ({ dbId, csvPath, descriptions }) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const trimmed = (dbId || "").trim();
    if (!trimmed) {
      throw new Error("DB ID를 입력해 주세요.");
    }
    return resolveJson(ttsApi("/db/convert"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        db_id: trimmed,
        csv_path: csvPath || undefined,
        descriptions: descriptions || {},
      }),
    });
  };

  const createNewSession = async () => {
    if (!currentUserId || sessionActionLoading) {
      return;
    }
    setSessionActionLoading(true);
    setSessionError("");
    try {
      const data = await resolveJson(ttsApi("/sessions"), {
        method: "POST",
      });
      const rawSessionId = data.session_id;
      const sessionId =
        typeof rawSessionId === "number" ? rawSessionId : Number(rawSessionId);
      if (Number.isFinite(sessionId) && sessionId > 0) {
        setCurrentSessionId(sessionId);
      }
      const dbId = typeof data.db_id === "string" ? data.db_id.trim() : "";
      setCurrentDbId(dbId);
      applySessionHistory(data.history);
      await fetchSessionList();
      if (window.innerWidth <= 1024) {
        setSessionsSidebarOpen(false);
      }
    } catch (error) {
      setSessionError(error.message || "새 세션 생성에 실패했습니다.");
    } finally {
      setSessionActionLoading(false);
    }
  };

  const selectSession = async (sessionId) => {
    const parsed =
      typeof sessionId === "number" ? sessionId : Number(sessionId);
    if (
      !currentUserId ||
      sessionActionLoading ||
      !Number.isFinite(parsed) ||
      parsed <= 0
    ) {
      return;
    }
    if (parsed === currentSessionId) {
      if (window.innerWidth <= 1024) {
        setSessionsSidebarOpen(false);
      }
      return;
    }
    setSessionActionLoading(true);
    setSessionError("");
    try {
      const data = await resolveJson(ttsApi("/sessions/select"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ session_id: parsed }),
      });
      const rawSessionId = data.session_id;
      const nextSessionId =
        typeof rawSessionId === "number" ? rawSessionId : Number(rawSessionId);
      if (Number.isFinite(nextSessionId) && nextSessionId > 0) {
        setCurrentSessionId(nextSessionId);
      }
      const dbId = typeof data.db_id === "string" ? data.db_id.trim() : "";
      setCurrentDbId(dbId);
      applySessionHistory(data.history);
      await fetchSessionList();
      if (window.innerWidth <= 1024) {
        setSessionsSidebarOpen(false);
      }
    } catch (error) {
      setSessionError(error.message || "세션 전환에 실패했습니다.");
    } finally {
      setSessionActionLoading(false);
    }
  };

  useEffect(() => {
    let active = true;

    const bootstrap = async () => {
      try {
        const me = await resolveJson("/api/auth/me");
        const rawUserId = typeof me?.user_id === "string" ? me.user_id : "";
        const userId = rawUserId.trim().toLowerCase();
        if (!userId) {
          throw new Error("로그인 사용자 ID를 확인할 수 없습니다.");
        }
        const username =
          typeof me?.username === "string" && me.username.trim()
            ? me.username.trim()
            : userId;
        if (!active) {
          return;
        }
        setCurrentUserId(userId);
        setCurrentUsername(username);
        setStatusMessage(`${username}님, 대화를 시작해 보세요.`);
        setAuthError("");
        setSqlMode(false);
        setCurrentDbId("");
        await loadHistory();
        await loadUserDb();
        await fetchSessionList(userId);
      } catch (error) {
        if (!active) {
          return;
        }
        setCurrentUserId("");
        setCurrentUsername("");
        setCurrentSessionId(null);
        setSessionList([]);
        setSessionError("");
        setProfileMenuOpen(false);
        setProfileActionLoading(false);
        setCurrentDbId("");
        setChatHistory([]);
        setLastAssistantSql("");
        setShowSqlButton(false);
        setStatusMessage("로그인 정보를 확인하지 못했습니다.");
        setAuthError(error.message || "로그인 정보를 확인하지 못했습니다.");
      }
    };

    bootstrap();
    return () => {
      active = false;
    };
  }, []);

  const handleSqlModeToggle = (nextValue) => {
    setSqlMode(Boolean(nextValue));
  };

  const sendMessage = async (message) => {
    const trimmed = message.trim();
    if (!trimmed || !currentUserId || loading) {
      return;
    }

    const userMessage = { role: "user", type: "text", content: trimmed };
    setChatHistory((prev) => {
      const next = [...prev, userMessage];
      if (sqlMode) {
        next.push({
          role: "assistant",
          type: "loading",
          content: SQL_LOADING_MESSAGE,
        });
      }
      return next;
    });
    setLoading(true);

    try {
      const data = await resolveJson(ttsApi("/chat"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          message: trimmed,
          mode: sqlMode ? "sql" : "chat",
        }),
      });

      const history = Array.isArray(data.history) ? mapHistoryItems(data.history) : [];
      if (history.length > 0) {
        setChatHistory(history);
        const sqlSnippet = getLastAssistantSql(history);
        setLastAssistantSql(sqlSnippet);
        setShowSqlButton(Boolean(sqlSnippet));
        setTableMoreLoading({});
      } else {
        const assistantResponse = data.response || "응답이 비어 있습니다.";
        const nextMessages = mapHistoryItems([
          { role: "assistant", content: assistantResponse },
        ]);
        setChatHistory((prev) => {
          const updated = [...stripSqlLoadingMessages(prev), ...nextMessages];
          const sqlSnippet = getLastAssistantSql(updated);
          setLastAssistantSql(sqlSnippet);
          setShowSqlButton(Boolean(sqlSnippet));
          return updated;
        });
      }
      await fetchSessionList();
    } catch (error) {
      setChatHistory((prev) => {
        const updated = [
          ...stripSqlLoadingMessages(prev),
          {
            role: "assistant",
            type: "text",
            content: error.message || "요청 처리 중 오류가 발생했습니다.",
          },
        ];

        return updated;
      });
      setSqlRunning(false);
      setSqlTesting(false);
    } finally {
      setLoading(false);
    }
  };

  const handleRunSql = async () => {
    if (!currentUserId || !lastAssistantSql || sqlRunning || sqlTesting) {
      return;
    }
    setSqlRunning(true);
    setTableMoreLoading({});
    try {
      const data = await resolveJson(ttsApi("/sql/execute"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          sql: lastAssistantSql,
        }),
      });

      if (Array.isArray(data.history) && data.history.length) {
        const mapped = mapHistoryItems(data.history);
        setChatHistory(mapped);

        const sqlSnippet = getLastAssistantSql(mapped);
        setLastAssistantSql(sqlSnippet);
        setShowSqlButton(Boolean(sqlSnippet));
        return;
      }

      const responseText =
        data.message || data.response || "SQL 실행 요청을 완료했습니다.";

      const nextMessages = [
        {
          role: "assistant",
          type: "text",
          content: responseText,
        },
      ];

      const tableHeaders =
        data.headers || data.table_headers || data.columns || [];
      const tableRows = data.rows || data.table_rows || [];
      const rawTableNumber = data.table_number;
      const parsedTableNumber =
        typeof rawTableNumber === "number"
          ? rawTableNumber
          : typeof rawTableNumber === "string" && rawTableNumber.trim()
            ? Number(rawTableNumber)
            : null;
      const tableNumber =
        parsedTableNumber !== null && Number.isFinite(parsedTableNumber)
          ? parsedTableNumber
          : null;
      const hasMore = Boolean(data.has_more);
      const csvPath = typeof data.csv_path === "string" ? data.csv_path : null;
      if (Array.isArray(tableHeaders) && Array.isArray(tableRows) && tableRows.length) {
        const normalizedHeaders = tableHeaders.map((header) => String(header));
        const normalizedRows = tableRows.map((row) =>
          Array.isArray(row)
            ? row.map((cell) => (cell == null ? "" : String(cell)))
            : [],
        );
        nextMessages.push({
          role: "assistant",
          type: "table",
          headers: normalizedHeaders,
          rows: normalizedRows,
          tableInfo: typeof data.table_info === "string" ? data.table_info : null,
          csv: typeof data.csv === "string" ? data.csv : null,
          csvPath,
          tableNumber,
          hasMore,
        });
      }

      const augmentationAnchor = lastAssistantSql;
      setChatHistory(prev => [...prev, ...nextMessages]);
      setShowSqlButton(Boolean(augmentationAnchor));
    } catch (error) {
      const augmentationAnchor = lastAssistantSql;
      setChatHistory(prev => [
        ...prev,
        {
          role: "assistant",
          type: "text",
          content: error.message || "SQL 실행 중 오류가 발생했습니다.",
        },
      ]);
    } finally {
      setSqlRunning(false);
    }
  };

  const handleTestSql = async () => {
    if (!currentUserId || !lastAssistantSql || sqlRunning || sqlTesting) {
      return;
    }
    setSqlTesting(true);
    setTableMoreLoading({});
    try {
      const data = await resolveJson(ttsApi("/sql/test"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          sql: lastAssistantSql,
        }),
      });

      if (Array.isArray(data.history) && data.history.length) {
        const mapped = mapHistoryItems(data.history);
        setChatHistory(mapped);

        const sqlSnippet = getLastAssistantSql(mapped);
        setLastAssistantSql(sqlSnippet);
        setShowSqlButton(Boolean(sqlSnippet));
        return;
      }

      const responseText =
        data.message || data.response || "SQL 테스트 요청을 완료했습니다.";

      const nextMessages = [
        {
          role: "assistant",
          type: "text",
          content: responseText,
        },
      ];

      const tableHeaders =
        data.headers || data.table_headers || data.columns || [];
      const tableRows = data.rows || data.table_rows || [];
      const rawTableNumber = data.table_number;
      const parsedTableNumber =
        typeof rawTableNumber === "number"
          ? rawTableNumber
          : typeof rawTableNumber === "string" && rawTableNumber.trim()
            ? Number(rawTableNumber)
            : null;
      const tableNumber =
        parsedTableNumber !== null && Number.isFinite(parsedTableNumber)
          ? parsedTableNumber
          : null;
      const hasMore = Boolean(data.has_more);
      const csvPath = typeof data.csv_path === "string" ? data.csv_path : null;
      if (Array.isArray(tableHeaders) && Array.isArray(tableRows) && tableRows.length) {
        const normalizedHeaders = tableHeaders.map((header) => String(header));
        const normalizedRows = tableRows.map((row) =>
          Array.isArray(row)
            ? row.map((cell) => (cell == null ? "" : String(cell)))
            : [],
        );
        nextMessages.push({
          role: "assistant",
          type: "table",
          headers: normalizedHeaders,
          rows: normalizedRows,
          tableInfo: typeof data.table_info === "string" ? data.table_info : null,
          csv: typeof data.csv === "string" ? data.csv : null,
          csvPath,
          tableNumber,
          hasMore,
        });
      }

      const augmentationAnchor = lastAssistantSql;
      setChatHistory(prev => [...prev, ...nextMessages]);
      setShowSqlButton(Boolean(augmentationAnchor));
    } catch (error) {
      setChatHistory(prev => [
        ...prev,
        {
          role: "assistant",
          type: "text",
          content: error.message || "SQL 테스트 중 오류가 발생했습니다.",
        },
      ]);
    } finally {
      setSqlTesting(false);
    }
  };

  const handleLoadMoreTable = async (tableNumber) => {
    if (!currentUserId || typeof tableNumber !== "number" || tableNumber <= 0) {
      return;
    }
    if (tableMoreLoading[tableNumber]) {
      return;
    }
    setTableMoreLoading((prev) => ({ ...prev, [tableNumber]: true }));
    try {
      const data = await resolveJson(ttsApi("/sql/table-more"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          table_number: tableNumber,
        }),
      });
      const nextRows = Array.isArray(data.rows)
        ? data.rows.map((row) =>
          Array.isArray(row)
            ? row.map((cell) => (cell == null ? "" : String(cell)))
            : [],
        )
        : [];
      const hasMore = Boolean(data.has_more);
      setChatHistory((prev) => {
        const updated = prev.map((item) => {
          if (
            item.role === "assistant" &&
            item.type === "table" &&
            item.tableNumber === tableNumber
          ) {
            const mergedRows = Array.isArray(item.rows)
              ? [...item.rows, ...nextRows]
              : [...nextRows];
            return {
              ...item,
              rows: mergedRows,
              hasMore,
            };
          }
          return item;
        });

        return updated;
      });
    } catch (error) {
      setChatHistory((prev) => {
        const updated = [
          ...prev,
          {
            role: "assistant",
            type: "text",
            content:
              error.message || "추가 테이블 데이터를 불러오는 중 오류가 발생했습니다.",
          },
        ];

        return updated;
      });
    } finally {
      setTableMoreLoading((prev) => {
        const next = { ...prev };
        delete next[tableNumber];
        return next;
      });
    }
  };

  const generateContextQuestions = async (topic) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const trimmed = (topic || "").trim();
    if (!trimmed) {
      throw new Error("정보 주제를 입력해 주세요.");
    }
    const data = await resolveJson(ttsApi("/context/questions"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        topic: trimmed,
      }),
    });
    const questions = Array.isArray(data.questions)
      ? data.questions.filter(
        (question) => typeof question === "string" && question.trim(),
      )
      : [];
    if (!questions.length) {
      throw new Error("추가 질문 생성에 실패했습니다.");
    }
    return questions;
  };

  const saveContextAnswers = async ({ topic, questions, answers }) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const sanitizedTopic = (topic || "").trim();
    if (!sanitizedTopic) {
      throw new Error("정보 주제를 입력해 주세요.");
    }
    const sanitizedQuestions = Array.isArray(questions)
      ? questions.map((question) => (typeof question === "string" ? question.trim() : ""))
      : [];
    const sanitizedAnswers = Array.isArray(answers)
      ? answers.map((answer) => (typeof answer === "string" ? answer.trim() : ""))
      : [];
    if (
      !sanitizedQuestions.length ||
      !sanitizedAnswers.length ||
      sanitizedQuestions.some((value) => !value) ||
      sanitizedAnswers.some((value) => !value)
    ) {
      throw new Error("질문과 답변을 모두 입력해 주세요.");
    }
    if (sanitizedQuestions.length !== sanitizedAnswers.length) {
      throw new Error("질문 수와 답변 수가 일치하지 않습니다.");
    }
    await resolveJson(ttsApi("/context"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        topic: sanitizedTopic,
        questions: sanitizedQuestions,
        answers: sanitizedAnswers,
      }),
    });
  };

  const saveFreeformContext = async ({ topic, content }) => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const sanitizedContent = (content || "").trim();
    if (!sanitizedContent) {
      throw new Error("바로 추가할 정보를 입력해 주세요.");
    }
    await resolveJson(ttsApi("/context/direct"), {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({
        topic: (topic || "").trim(),
        content: sanitizedContent,
      }),
    });
  };

  const fetchContextNotes = async () => {
    if (!currentUserId) {
      throw new Error("로그인 정보를 확인 중입니다.");
    }
    const data = await resolveJson(ttsApi("/context"));
    const notes = Array.isArray(data.notes)
      ? data.notes.filter((note) => note && typeof note === "object")
      : [];
    return notes;
  };

  const profileName = (currentUsername || currentUserId || "사용자").trim();
  const profileHandle = currentUserId ? `@${currentUserId}` : "로그인 필요";
  const profileInitial = getProfileInitial(profileName, currentUserId);
  const themeToggleLabel = themeMode === "dark" ? "밝은 모드로 전환" : "다크 모드로 전환";
  const bugDescriptionLength = bugReportDescription.length;
  const frontendVersion = String(import.meta.env.VITE_APP_VERSION || "web-ui");

  const closeBugReportModal = () => {
    if (bugReportSubmitting) {
      return;
    }
    setBugReportModalOpen(false);
    setBugReportError("");
    setBugReportSuccess("");
  };

  const openBugReportModal = () => {
    setHelpSubmenuOpen(false);
    setProfileMenuOpen(false);
    setBugReportError("");
    setBugReportSuccess("");
    setBugReportModalOpen(true);
  };

  const clearHelpSubmenuCloseTimer = () => {
    if (helpSubmenuCloseTimerRef.current) {
      window.clearTimeout(helpSubmenuCloseTimerRef.current);
      helpSubmenuCloseTimerRef.current = null;
    }
  };

  const openHelpSubmenu = () => {
    clearHelpSubmenuCloseTimer();
    setHelpSubmenuOpen(true);
  };

  const scheduleCloseHelpSubmenu = () => {
    clearHelpSubmenuCloseTimer();
    helpSubmenuCloseTimerRef.current = window.setTimeout(() => {
      setHelpSubmenuOpen(false);
      helpSubmenuCloseTimerRef.current = null;
    }, 220);
  };

  const submitBugReport = async () => {
    if (!currentUserId || bugReportSubmitting) {
      return;
    }
    const description = (bugReportDescription || "").trim();
    if (!description) {
      setBugReportError("어떤 문제가 발생했는지 입력해 주세요.");
      setBugReportSuccess("");
      return;
    }
    if (description.length > BUG_REPORT_MAX_LENGTH) {
      setBugReportError(`버그 설명은 ${BUG_REPORT_MAX_LENGTH}자 이하로 입력해 주세요.`);
      setBugReportSuccess("");
      return;
    }
    setBugReportSubmitting(true);
    setBugReportError("");
    setBugReportSuccess("");
    try {
      await resolveJson(ttsApi("/bug-reports"), {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          description,
          version: frontendVersion.slice(0, 64),
          browser: String(window.navigator.userAgent || "unknown").slice(0, 128),
        }),
      });
      setBugReportSuccess("");
      setBugReportDescription("");
      setBugReportModalOpen(false);
      setSystemToastMessage("버그 리포트 감사합니다. 빠른 확인 후 조치하겠습니다.");
    } catch (error) {
      setBugReportError(error.message || "버그 신고 전송에 실패했습니다.");
    } finally {
      setBugReportSubmitting(false);
    }
  };

  const handleSidebarProfileAction = async (action) => {
    setProfileMenuOpen(false);
    clearHelpSubmenuCloseTimer();
    setHelpSubmenuOpen(false);
    if (action === "toggle-theme") {
      setThemeMode((prev) => (prev === "dark" ? "light" : "dark"));
      return;
    }
    if (action === "open-bug-report") {
      openBugReportModal();
      return;
    }
    if (action === "logout") {
      if (profileActionLoading) {
        return;
      }
      setProfileActionLoading(true);
      try {
        await fetch("/api/auth/logout", {
          method: "POST",
          credentials: "include",
        });
      } catch (_error) {
        // 로그아웃 요청 실패 시에도 로그인 화면으로 보낸다.
      } finally {
        window.location.href = "/login";
      }
      return;
    }

    if (action === "help") {
      window.location.href = "/login";
      return;
    }
    window.location.href = "/login/mypage";
  };

  useEffect(() => {
    if (!sessionsSidebarOpen) {
      setProfileMenuOpen(false);
      clearHelpSubmenuCloseTimer();
      setHelpSubmenuOpen(false);
    }
  }, [sessionsSidebarOpen]);

  useEffect(() => {
    try {
      window.localStorage.setItem("tts-theme-mode", themeMode);
    } catch (_error) {
      // 저장 실패 시 무시
    }
  }, [themeMode]);

  useEffect(() => {
    if (!profileMenuOpen) {
      clearHelpSubmenuCloseTimer();
      setHelpSubmenuOpen(false);
      return undefined;
    }
    const handlePointerDown = (event) => {
      if (profileMenuRef.current && !profileMenuRef.current.contains(event.target)) {
        setProfileMenuOpen(false);
      }
    };
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        setProfileMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [profileMenuOpen]);

  useEffect(
    () => () => {
      clearHelpSubmenuCloseTimer();
    },
    [],
  );

  useEffect(() => {
    if (!bugReportModalOpen) {
      return undefined;
    }
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        closeBugReportModal();
      }
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [bugReportModalOpen, bugReportSubmitting]);

  useEffect(() => {
    if (!systemToastMessage) {
      return undefined;
    }
    const timeout = window.setTimeout(() => {
      setSystemToastMessage("");
    }, 2600);
    return () => {
      window.clearTimeout(timeout);
    };
  }, [systemToastMessage]);

  const emptyMessage = currentUserId
    ? "안녕하세요! 무엇을 도와드릴까요?"
    : "로그인 정보를 확인하는 중입니다.";

  // Dashboard Logic: Extract images and tables, and filter history
  const { allImages, allTables, filteredHistory } = useMemo(() => {
    if (!Array.isArray(chatHistory)) {
      return { allImages: [], allTables: [], filteredHistory: [] };
    }

    const historyForChat = [];
    const images = [];
    const tables = [];

    chatHistory.forEach((item) => {
      if (item.type === "image") {
        images.push(item);
      } else if (item.type === "table") {
        tables.push(item);
      } else {
        historyForChat.push(item);
      }
    });

    return {
      allImages: images,
      allTables: tables,
      filteredHistory: historyForChat,
    };
  }, [chatHistory]);

  // State for tracking active item index
  const [activeImageIndex, setActiveImageIndex] = useState(-1);
  const [activeTableIndex, setActiveTableIndex] = useState(-1);

  // Resize Logic
  const [leftRatio, setLeftRatio] = useState(40);
  const [plotRatio, setPlotRatio] = useState(50);
  const contentSplitRef = useRef(null);
  const leftPanelRef = useRef(null);
  const isResizingRef = useRef(false);
  const isVResizingRef = useRef(false);

  const handleResizeStart = (e) => {
    isResizingRef.current = true;
    document.addEventListener("mousemove", handleResizeMove);
    document.addEventListener("mouseup", handleResizeEnd);
    // Prevent text selection while dragging
    document.body.style.userSelect = "none";
  };

  const handleResizeMove = (e) => {
    if (!isResizingRef.current || !contentSplitRef.current) return;

    const containerRect = contentSplitRef.current.getBoundingClientRect();
    const newLeftWidth = e.clientX - containerRect.left;
    let newRatio = (newLeftWidth / containerRect.width) * 100;

    // Clamp ratio (min 20%, max 80%)
    if (newRatio < 20) newRatio = 20;
    if (newRatio > 80) newRatio = 80;

    setLeftRatio(newRatio);
  };

  const handleResizeEnd = () => {
    isResizingRef.current = false;
    document.removeEventListener("mousemove", handleResizeMove);
    document.removeEventListener("mouseup", handleResizeEnd);
    document.body.style.userSelect = "";
  };

  const handleVResizeStart = (e) => {
    e.stopPropagation(); // Prevent affecting parent resize
    isVResizingRef.current = true;
    document.addEventListener("mousemove", handleVResizeMove);
    document.addEventListener("mouseup", handleVResizeEnd);
    document.body.style.userSelect = "none";
  };

  const handleVResizeMove = (e) => {
    if (!isVResizingRef.current || !leftPanelRef.current) return;

    const containerRect = leftPanelRef.current.getBoundingClientRect();
    const newTopHeight = e.clientY - containerRect.top;
    let newRatio = (newTopHeight / containerRect.height) * 100;

    // Clamp ratio (min 20%, max 80%)
    if (newRatio < 20) newRatio = 20;
    if (newRatio > 80) newRatio = 80;

    setPlotRatio(newRatio);
  };

  const handleVResizeEnd = () => {
    isVResizingRef.current = false;
    document.removeEventListener("mousemove", handleVResizeMove);
    document.removeEventListener("mouseup", handleVResizeEnd);
    document.body.style.userSelect = "";
  };

  useEffect(() => {
    if (allImages.length > 0) {
      // If index is invalid or -1, snap to last
      setActiveImageIndex((prev) => (prev === -1 || prev >= allImages.length ? allImages.length - 1 : prev));
      // Optionally snap to last if new item added? 
      // Current logic: if length changes, snap to last? 
      // Let's simpler: Always snap to last if length grows.
      // We need a ref to track previous length to know if it *grew*.
    } else {
      setActiveImageIndex(-1);
    }
  }, [allImages.length]); // Dependency on length only to avoid loops

  useEffect(() => {
    if (allTables.length > 0) {
      setActiveTableIndex((prev) => (prev === -1 || prev >= allTables.length ? allTables.length - 1 : prev));
    } else {
      setActiveTableIndex(-1);
    }
  }, [allTables.length]);

  // Handlers for navigation
  const handlePrevImage = () => setActiveImageIndex((prev) => Math.max(0, prev - 1));
  const handleNextImage = () => setActiveImageIndex((prev) => Math.min(allImages.length - 1, prev + 1));
  const handlePrevTable = () => setActiveTableIndex((prev) => Math.max(0, prev - 1));
  const handleNextTable = () => setActiveTableIndex((prev) => Math.min(allTables.length - 1, prev + 1));

  // Helper to get current item safely
  const currentImage = allImages[activeImageIndex] || null;
  const currentTable = allTables[activeTableIndex] || null;

  return (
    <div className={`app-layout${themeMode === "light" ? " light-theme" : ""}`}>
      {/* Global Header */}
      <header className="global-header">
        <div className="header-content">
          <button
            type="button"
            className="session-toggle-button"
            aria-label="세션 사이드바 열기"
            aria-expanded={sessionsSidebarOpen}
            onClick={() => setSessionsSidebarOpen((prev) => !prev)}
          >
            <span className="session-toggle-icon" />
            <span className="session-toggle-icon" />
            <span className="session-toggle-icon" />
          </button>
          <section className="user-setup">
            <p className={`user-status${authError ? " user-status--error" : ""}`}>
              {authError || statusMessage}
            </p>
          </section>
        </div>
      </header>

      <div className="app-body">
        <aside className={`session-sidebar${sessionsSidebarOpen ? " open" : ""}`}>
          <div className="session-sidebar-header">
            <h2>대화</h2>
            <button
              type="button"
              className="session-new-button"
              onClick={createNewSession}
              disabled={!currentUserId || sessionActionLoading}
            >
              + 새 세션
            </button>
          </div>
          <div className="session-sidebar-content">
            {sessionError && (
              <p className="session-sidebar-error">{sessionError}</p>
            )}
            {sessionsLoading ? (
              <p className="session-sidebar-loading">세션 목록을 불러오는 중...</p>
            ) : sessionList.length === 0 ? (
              <p className="session-sidebar-empty">세션이 없습니다.</p>
            ) : (
              <div className="session-list">
                {sessionList.map((session) => {
                  const isActive = session.session_id === currentSessionId;
                  return (
                    <button
                      type="button"
                      key={session.session_id}
                      className={`session-list-item${isActive ? " active" : ""}`}
                      onClick={() => selectSession(session.session_id)}
                      disabled={sessionActionLoading}
                    >
                      <span className="session-list-title">{session.title}</span>
                      <span className="session-list-preview">
                        {session.preview || "메시지가 없습니다."}
                      </span>
                      <span className="session-list-meta">
                        {formatSessionTimestamp(session.last_accessed)} · {session.message_count}
                      </span>
                    </button>
                  );
                })}
              </div>
            )}
          </div>
          <div className="session-sidebar-account" ref={profileMenuRef}>
            {profileMenuOpen && (
              <div className="session-account-menu" role="menu" aria-label="계정 메뉴">
                <button
                  type="button"
                  className="session-account-menu-item"
                  onClick={() => handleSidebarProfileAction("plan")}
                >
                  <span className="session-account-menu-icon session-account-menu-icon--plan" aria-hidden="true" />
                  <span>플랜 업그레이드</span>
                </button>
                <button
                  type="button"
                  className="session-account-menu-item"
                  onClick={() => handleSidebarProfileAction("personal")}
                >
                  <span className="session-account-menu-icon session-account-menu-icon--personal" aria-hidden="true" />
                  <span>개인 맞춤 설정</span>
                </button>
                <button
                  type="button"
                  className="session-account-menu-item"
                  onClick={() => handleSidebarProfileAction("toggle-theme")}
                >
                  <span className="session-account-menu-icon session-account-menu-icon--theme" aria-hidden="true" />
                  <span>{themeToggleLabel}</span>
                </button>
                <button
                  type="button"
                  className="session-account-menu-item"
                  onClick={() => handleSidebarProfileAction("settings")}
                >
                  <span className="session-account-menu-icon session-account-menu-icon--settings" aria-hidden="true" />
                  <span>설정</span>
                </button>
                <div className="session-account-menu-divider" />
                <div
                  className="session-account-help-group"
                  onMouseEnter={openHelpSubmenu}
                  onMouseLeave={scheduleCloseHelpSubmenu}
                >
                  <button
                    type="button"
                    className="session-account-menu-item session-account-menu-item--with-arrow"
                    onFocus={openHelpSubmenu}
                    onClick={() => {
                      if (helpSubmenuOpen) {
                        clearHelpSubmenuCloseTimer();
                        setHelpSubmenuOpen(false);
                      } else {
                        openHelpSubmenu();
                      }
                    }}
                    aria-haspopup="menu"
                    aria-expanded={helpSubmenuOpen}
                  >
                    <span className="session-account-menu-icon session-account-menu-icon--help" aria-hidden="true" />
                    <span>도움말</span>
                    <span className="session-account-menu-arrow" aria-hidden="true">›</span>
                  </button>
                  {helpSubmenuOpen && (
                    <div
                      className="session-help-submenu"
                      role="menu"
                      aria-label="도움말 메뉴"
                      onMouseEnter={openHelpSubmenu}
                      onMouseLeave={scheduleCloseHelpSubmenu}
                    >
                      <button
                        type="button"
                        className="session-help-submenu-item"
                        onClick={() => handleSidebarProfileAction("open-bug-report")}
                      >
                        버그 신고
                      </button>
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  className="session-account-menu-item session-account-menu-item--danger"
                  onClick={() => handleSidebarProfileAction("logout")}
                  disabled={profileActionLoading}
                >
                  <span className="session-account-menu-icon session-account-menu-icon--logout" aria-hidden="true" />
                  <span>{profileActionLoading ? "로그아웃 중..." : "로그아웃"}</span>
                </button>
              </div>
            )}
            <button
              type="button"
              className={`session-account-trigger${profileMenuOpen ? " open" : ""}`}
              onClick={() => setProfileMenuOpen((prev) => !prev)}
              disabled={!currentUserId || profileActionLoading}
              aria-haspopup="menu"
              aria-expanded={profileMenuOpen}
            >
              <span className="session-account-avatar">{profileInitial}</span>
              <span className="session-account-text">
                <span className="session-account-name">{profileName}</span>
                <span className="session-account-handle">{profileHandle}</span>
              </span>
              <span className="session-account-tier">Plus</span>
            </button>
          </div>
        </aside>

        {/* Main Content Area */}
        {/* Main Content Area */}
        <div
          className={`content-split${sessionsSidebarOpen ? " sidebar-open" : ""}`}
          ref={contentSplitRef}
        >

        {/* Left Panel: Dashboard (Plot & Table) */}
        {/* Left Panel: Dashboard (Plot & Table) */}
        <div className="left-panel" ref={leftPanelRef} style={{ flex: leftRatio }}>
          {/* Top: Plot Section */}
          <div className="dashboard-plot-section" style={{ flex: plotRatio }}>
            <div className="dashboard-section-title">
              <span>PLOT {allImages.length > 0 && `(${activeImageIndex + 1}/${allImages.length})`}</span>
              <div className="dashboard-nav-controls">
                <button
                  disabled={activeImageIndex <= 0}
                  onClick={handlePrevImage}
                  className="nav-btn"
                >◀</button>
                <button
                  disabled={activeImageIndex >= allImages.length - 1}
                  onClick={handleNextImage}
                  className="nav-btn"
                >▶</button>
              </div>
            </div>
            <div className="dashboard-content">
              {currentImage ? (
                <img src={currentImage.src} alt={currentImage.alt || "Plot"} />
              ) : (
                <div className="dashboard-empty">생성된 이미지가 없습니다</div>
              )}
            </div>
          </div>

          <div className="resize-handle-vertical" onMouseDown={handleVResizeStart} />

          {/* Bottom: Table Section */}
          <div className="dashboard-table-section" style={{ flex: 100 - plotRatio }}>
            <div className="dashboard-section-title">
              <span>TABLE {allTables.length > 0 && `(${activeTableIndex + 1}/${allTables.length})`}</span>
              {currentTable && currentTable.tableNumber && (
                <span style={{ fontSize: "0.9rem", opacity: 0.7, marginRight: 'auto', marginLeft: '10px' }}>
                  #{currentTable.tableNumber}
                </span>
              )}
              <div className="dashboard-nav-controls">
                <button
                  disabled={activeTableIndex <= 0}
                  onClick={handlePrevTable}
                  className="nav-btn"
                >◀</button>
                <button
                  disabled={activeTableIndex >= allTables.length - 1}
                  onClick={handleNextTable}
                  className="nav-btn"
                >▶</button>
              </div>
            </div>
            <div className="dashboard-content">
              {currentTable ? (
                <MessageList
                  messages={[currentTable]}
                  emptyMessage=""
                  onTableLoadMore={handleLoadMoreTable}
                  loadingTables={tableMoreLoading}
                />
              ) : (
                <div className="dashboard-empty">데이터가 없습니다</div>
              )}
            </div>
          </div>
        </div>

        {/* Resize Handle */}
        <div className="resize-handle" onMouseDown={handleResizeStart} />

        {/* Right Panel: Chat Interface */}
        <div className="right-panel" style={{ flex: 100 - leftRatio }}>
          <div className="chat-container">
            <main className="chat-window" id="chat-window">
              <MessageList
                messages={filteredHistory}
                emptyMessage={emptyMessage}
                onTableLoadMore={handleLoadMoreTable}
                loadingTables={tableMoreLoading}
              />
            </main>

            <ChatInput
              disabled={!currentUserId}
              loading={loading}
              onSend={sendMessage}
              sqlMode={sqlMode}
              onToggleSqlMode={handleSqlModeToggle}
              clarificationState={null}
              showSqlButton={showSqlButton}
              sqlRunLoading={sqlRunning}
              onRunSql={handleRunSql}
              sqlTestLoading={sqlTesting}
              onTestSql={handleTestSql}
              onUploadDb={uploadDbCsv}
              onConvertDb={convertDbCsv}
              canAddContext={Boolean(currentUserId)}
              onGenerateContextQuestions={generateContextQuestions}
              onSaveContextAnswers={saveContextAnswers}
              onSaveFreeformContext={saveFreeformContext}
              onFetchContextNotes={fetchContextNotes}
              currentDbId={currentDbId}
              onFetchDbList={fetchDbList}
              onChangeDb={changeDb}
            />
          </div>
        </div>
      </div>
      </div>
      {systemToastMessage && (
        <div className="system-toast system-toast-success" role="status" aria-live="polite">
          {systemToastMessage}
        </div>
      )}
      {bugReportModalOpen && (
        <div
          className="context-modal-backdrop bug-report-modal-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label="버그 신고"
          onClick={closeBugReportModal}
        >
          <div
            className="context-modal bug-report-modal"
            onClick={(event) => event.stopPropagation()}
          >
            <div className="context-modal-header">
              <div className="context-modal-title">
                <h2>무슨 일이 일어났나요?</h2>
              </div>
              <button
                type="button"
                className="context-close-button"
                aria-label="버그 신고 닫기"
                onClick={closeBugReportModal}
                disabled={bugReportSubmitting}
              >
                ×
              </button>
            </div>
            <textarea
              className="context-modal-textarea bug-report-textarea"
              value={bugReportDescription}
              onChange={(event) => {
                setBugReportDescription(event.target.value.slice(0, BUG_REPORT_MAX_LENGTH));
                setBugReportError("");
                setBugReportSuccess("");
              }}
              placeholder="어떤 문제가 일어났는지 알려주세요"
              maxLength={BUG_REPORT_MAX_LENGTH}
              rows={9}
              disabled={bugReportSubmitting}
            />
            <p className="bug-report-counter">
              {bugDescriptionLength}/{BUG_REPORT_MAX_LENGTH}자 사용
            </p>
            {bugReportError && (
              <p className="context-modal-feedback error">{bugReportError}</p>
            )}
            {bugReportSuccess && (
              <p className="context-modal-feedback success">{bugReportSuccess}</p>
            )}
            <div className="context-modal-actions bug-report-actions">
              <button
                type="button"
                className="context-primary-button bug-report-submit"
                onClick={submitBugReport}
                disabled={bugReportSubmitting}
              >
                {bugReportSubmitting ? "전송 중..." : "보내기"}
              </button>
            </div>
          </div>
        </div>
      )}
      {sessionsSidebarOpen && (
        <button
          type="button"
          className="session-sidebar-backdrop"
          onClick={() => setSessionsSidebarOpen(false)}
          aria-label="세션 사이드바 닫기"
        />
      )}
    </div>
  );
}
