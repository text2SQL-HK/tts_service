import { useEffect, useRef, useState } from "react";

const getScrollHeight = (element) => {
  if (!element) {
    return 0;
  }
  const savedValue = element.value;
  element.value = "";
  const baseScrollHeight = element.scrollHeight;
  element.value = savedValue;
  return baseScrollHeight;
};

const autoExpandTextarea = (element) => {
  if (!element) {
    return;
  }
  const minRows = Number(element.getAttribute("data-min-rows")) || 1;
  if (!element._baseScrollHeight) {
    element._baseScrollHeight = getScrollHeight(element);
  }
  const lineHeight =
    parseInt(
      (typeof window !== "undefined" &&
        window.getComputedStyle(element).lineHeight) ||
      "16",
      10,
    ) || 16;

  element.rows = minRows;
  const rows = Math.ceil(
    (element.scrollHeight - element._baseScrollHeight) / lineHeight,
  );
  element.rows = minRows + rows;
};

const parseCsvPreview = (file, maxRows = 3) =>
  new Promise((resolve, reject) => {
    if (!file) {
      resolve({ headers: [], rows: [] });
      return;
    }
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("CSV 파일을 읽을 수 없습니다."));
    reader.onload = () => {
      const text = typeof reader.result === "string" ? reader.result : "";
      const rows = [];
      let row = [];
      let field = "";
      let inQuotes = false;
      const limit = maxRows + 1;
      for (let i = 0; i < text.length; i += 1) {
        const char = text[i];
        if (inQuotes) {
          if (char === '"') {
            if (text[i + 1] === '"') {
              field += '"';
              i += 1;
            } else {
              inQuotes = false;
            }
          } else {
            field += char;
          }
        } else if (char === '"') {
          inQuotes = true;
        } else if (char === ",") {
          row.push(field);
          field = "";
        } else if (char === "\n" || char === "\r") {
          if (char === "\r" && text[i + 1] === "\n") {
            i += 1;
          }
          row.push(field);
          field = "";
          rows.push(row);
          row = [];
          if (rows.length >= limit) {
            break;
          }
        } else {
          field += char;
        }
      }
      if (rows.length < limit && (field.length > 0 || row.length > 0)) {
        row.push(field);
        rows.push(row);
      }
      const headers = rows[0] || [];
      if (headers[0] && headers[0].charCodeAt(0) === 0xfeff) {
        headers[0] = headers[0].slice(1);
      }
      const previewRows = rows.slice(1, maxRows + 1);
      resolve({ headers, rows: previewRows });
    };
    const blob = file.slice(0, 256 * 1024);
    reader.readAsText(blob, "utf-8");
  });

const DB_NAME_PATTERN = /^[A-Za-z0-9_]+$/;
const DB_NAME_START_PATTERN = /^[A-Za-z_]/;

const getDbNameError = (value) => {
  const trimmed = (value || "").trim();
  if (!trimmed) {
    return "DB 이름을 입력해 주세요.";
  }
  if (!DB_NAME_PATTERN.test(trimmed)) {
    return "DB 이름은 영문, 숫자, '_'만 사용할 수 있어요.";
  }
  if (!DB_NAME_START_PATTERN.test(trimmed)) {
    return "DB 이름은 숫자로 시작할 수 없어요.";
  }
  return "";
};

export default function ChatInput({
  disabled,
  loading,
  onSend,
  sqlMode,
  onToggleSqlMode,
  clarificationState,
  showSqlButton = false,
  sqlRunLoading = false,
  sqlTestLoading = false,
  onRunSql = () => { },
  onTestSql = () => { },
  onUploadDb = async () => ({}),
  onConvertDb = async () => ({}),
  canAddContext = false,
  onGenerateContextQuestions = async () => [],
  onSaveContextAnswers = async () => { },
  onFetchContextNotes = async () => [],
  onSaveFreeformContext = async () => { },
  currentDbId = "",
  onFetchDbList = async () => [],
  onChangeDb = async () => { },
}) {
  const [message, setMessage] = useState("");
  const [isContextModalOpen, setIsContextModalOpen] = useState(false);
  const [contextTopic, setContextTopic] = useState("");
  const [contextFreeform, setContextFreeform] = useState("");
  const [generatedQuestions, setGeneratedQuestions] = useState([]);
  const [contextAnswers, setContextAnswers] = useState([]);
  const [contextStage, setContextStage] = useState("topic");
  const [contextGenerating, setContextGenerating] = useState(false);
  const [contextSubmitting, setContextSubmitting] = useState(false);
  const [contextFeedback, setContextFeedback] = useState(null);
  const [contextNotes, setContextNotes] = useState([]);
  const [contextNotesLoading, setContextNotesLoading] = useState(false);
  const [contextNotesError, setContextNotesError] = useState(null);
  const [isDbModalOpen, setIsDbModalOpen] = useState(false);
  const [dbList, setDbList] = useState([]);
  const [dbLoading, setDbLoading] = useState(false);
  const [dbSubmitting, setDbSubmitting] = useState(false);
  const [dbError, setDbError] = useState(null);
  const [dbSelection, setDbSelection] = useState("");
  const [isDbDropdownOpen, setIsDbDropdownOpen] = useState(false);
  const [dbActiveIndex, setDbActiveIndex] = useState(-1);
  const [isDbUploadOpen, setIsDbUploadOpen] = useState(false);
  const [dbUploadFile, setDbUploadFile] = useState(null);
  const [dbUploadName, setDbUploadName] = useState("");
  const [dbUploadNameError, setDbUploadNameError] = useState("");
  const [dbUploadError, setDbUploadError] = useState(null);
  const [dbUploadSubmitting, setDbUploadSubmitting] = useState(false);
  const [dbUploadSuccess, setDbUploadSuccess] = useState("");
  const [isDbConvertOpen, setIsDbConvertOpen] = useState(false);
  const [dbConvertPreview, setDbConvertPreview] = useState({ headers: [], rows: [] });
  const [dbColumnDescriptions, setDbColumnDescriptions] = useState([]);
  const [dbTableDescription, setDbTableDescription] = useState("");
  const [dbConvertInfo, setDbConvertInfo] = useState({ dbId: "", csvPath: "" });
  const [dbConvertError, setDbConvertError] = useState(null);
  const [dbConvertSubmitting, setDbConvertSubmitting] = useState(false);
  const [dbConvertSuccess, setDbConvertSuccess] = useState("");
  const dbDropdownRef = useRef(null);
  const dbTriggerRef = useRef(null);
  const dbListRef = useRef(null);
  const dbUploadInputRef = useRef(null);
  const messageInputRef = useRef(null);

  const closeDbDropdown = ({ focusTrigger = true } = {}) => {
    setIsDbDropdownOpen(false);
    setDbActiveIndex(-1);
    if (focusTrigger) {
      setTimeout(() => {
        dbTriggerRef.current?.focus();
      }, 0);
    }
  };

  const openDbDropdown = (initialIndex) => {
    if (dbLoading || !dbList.length) {
      return;
    }
    let nextIndex = typeof initialIndex === "number" ? initialIndex : -1;
    if (nextIndex < 0 || nextIndex >= dbList.length) {
      const currentIndex = dbList.findIndex((item) => item === dbSelection);
      nextIndex = currentIndex >= 0 ? currentIndex : 0;
    }
    setDbActiveIndex(nextIndex);
    setIsDbDropdownOpen(true);
  };

  const toggleDbDropdown = () => {
    if (dbLoading || !dbList.length) {
      return;
    }
    if (isDbDropdownOpen) {
      closeDbDropdown();
    } else {
      openDbDropdown();
    }
  };

  const handleDbOptionSelect = (dbId) => {
    setDbSelection(dbId);
    closeDbDropdown();
  };

  const handleDbTriggerKeyDown = (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const targetIndex =
        event.key === "ArrowUp" && dbList.length ? dbList.length - 1 : 0;
      openDbDropdown(targetIndex);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (isDbDropdownOpen) {
        closeDbDropdown();
      } else {
        openDbDropdown();
      }
    }
  };

  const handleDbDropdownKeyDown = (event) => {
    if (!isDbDropdownOpen) {
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (!dbList.length) {
        return;
      }
      setDbActiveIndex((prev) => {
        const base = prev >= 0 ? prev : 0;
        const next = base + 1 >= dbList.length ? 0 : base + 1;
        setTimeout(() => {
          const option = document.getElementById(`db-option-${next}`);
          option?.scrollIntoView({ block: "nearest" });
        }, 0);
        return next;
      });
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      if (!dbList.length) {
        return;
      }
      setDbActiveIndex((prev) => {
        const next =
          prev <= 0 || prev === -1 ? dbList.length - 1 : prev - 1;
        setTimeout(() => {
          const option = document.getElementById(`db-option-${next}`);
          option?.scrollIntoView({ block: "nearest" });
        }, 0);
        return next;
      });
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      if (dbActiveIndex >= 0 && dbActiveIndex < dbList.length) {
        handleDbOptionSelect(dbList[dbActiveIndex]);
      }
    } else if (event.key === "Escape") {
      event.preventDefault();
      closeDbDropdown();
    } else if (event.key === "Tab") {
      closeDbDropdown({ focusTrigger: false });
    }
  };

  useEffect(() => {
    if (!isDbDropdownOpen) {
      return undefined;
    }
    const handlePointerDown = (event) => {
      if (
        dbDropdownRef.current &&
        !dbDropdownRef.current.contains(event.target) &&
        dbTriggerRef.current &&
        !dbTriggerRef.current.contains(event.target)
      ) {
        closeDbDropdown({ focusTrigger: false });
      }
    };
    const handleEscape = (event) => {
      if (event.key === "Escape") {
        closeDbDropdown();
      }
    };
    document.addEventListener("mousedown", handlePointerDown);
    document.addEventListener("touchstart", handlePointerDown);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      document.removeEventListener("touchstart", handlePointerDown);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [isDbDropdownOpen]);

  useEffect(() => {
    if (!isDbDropdownOpen) {
      return;
    }
    setTimeout(() => {
      dbListRef.current?.focus();
    }, 0);
  }, [isDbDropdownOpen]);

  useEffect(() => {
    if (!isDbModalOpen) {
      closeDbDropdown({ focusTrigger: false });
    }
  }, [isDbModalOpen]);

  useEffect(() => {
    if (!isDbDropdownOpen) {
      return;
    }
    const currentIndex = dbList.findIndex((item) => item === dbSelection);
    if (currentIndex >= 0) {
      setDbActiveIndex(currentIndex);
    } else if (dbList.length) {
      setDbActiveIndex(0);
    } else {
      setDbActiveIndex(-1);
    }
  }, [dbList, dbSelection, isDbDropdownOpen]);

  const handleSubmit = (event) => {
    event.preventDefault();
    const trimmed = message.trim();
    if (!trimmed || disabled || loading) {
      return;
    }
    onSend(trimmed);
    setMessage("");
  };

  const handleMessageChange = (event) => {
    setMessage(event.target.value);
    autoExpandTextarea(event.target);
  };

  const handleMessageKeyDown = (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      handleSubmit(event);
    }
  };

  const clarificationActive =
    clarificationState && typeof clarificationState === "object";
  const placeholder = (() => {
    if (sqlMode || clarificationActive) {
      if (clarificationActive) {
        const index = clarificationState.index || 1;
        const total = clarificationState.total || 1;
        return `추가 질문 ${index}/${total}에 대한 답변을 입력하세요...`;
      }
      return "분석할 질문을 입력하세요...";
    }
    return "질문을 입력하세요...";
  })();

  useEffect(() => {
    autoExpandTextarea(messageInputRef.current);
  }, [message]);

  const loadContextNotes = async () => {
    setContextNotesError(null);
    setContextNotesLoading(true);
    try {
      const notes = await onFetchContextNotes();
      setContextNotes(notes);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "저장된 정보를 불러오는 중 오류가 발생했습니다.";
      setContextNotesError(message);
    } finally {
      setContextNotesLoading(false);
    }
  };

  const loadDbOptions = async () => {
    setDbError(null);
    setDbLoading(true);
    try {
      const list = await onFetchDbList();
      const normalized = Array.isArray(list)
        ? list
          .map((item) => (typeof item === "string" ? item.trim() : ""))
          .filter(Boolean)
        : [];
      setDbList(normalized);
      const trimmedCurrent = (currentDbId || "").trim();
      const defaultSelection = normalized.includes(trimmedCurrent)
        ? trimmedCurrent
        : normalized[0] || "";
      setDbSelection(defaultSelection);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "DB 목록을 불러오는 중 오류가 발생했습니다.";
      setDbError(message);
    } finally {
      setDbLoading(false);
    }
  };

  const openContextModal = () => {
    setIsContextModalOpen(true);
    setContextStage("topic");
    setContextTopic("");
    setContextFreeform("");
    setGeneratedQuestions([]);
    setContextAnswers([]);
    setContextFeedback(null);
    setContextGenerating(false);
    setContextSubmitting(false);
    setContextNotes([]);
    setContextNotesError(null);
    loadContextNotes();
  };

  const openDbModal = () => {
    setIsDbModalOpen(true);
    setDbError(null);
    setDbSelection("");
    setDbList([]);
    setDbSubmitting(false);
    setIsDbDropdownOpen(false);
    setDbActiveIndex(-1);
    loadDbOptions();
  };

  const openDbUploadModal = () => {
    closeDbModal();
    setIsDbUploadOpen(true);
    setDbUploadFile(null);
    setDbUploadName("");
    setDbUploadNameError("");
    setDbUploadError(null);
    setDbUploadSuccess("");
    if (dbUploadInputRef.current) {
      dbUploadInputRef.current.value = "";
    }
  };

  const closeContextModal = () => {
    setIsContextModalOpen(false);
    setContextSubmitting(false);
    setContextTopic("");
    setContextFreeform("");
    setGeneratedQuestions([]);
    setContextAnswers([]);
    setContextStage("topic");
    setContextFeedback(null);
    setContextGenerating(false);
    setContextNotes([]);
    setContextNotesError(null);
    setContextNotesLoading(false);
  };

  const closeDbModal = () => {
    closeDbDropdown({ focusTrigger: false });
    setIsDbModalOpen(false);
    setDbLoading(false);
    setDbSubmitting(false);
    setDbError(null);
    setIsDbDropdownOpen(false);
  };

  const closeDbUploadModal = () => {
    setIsDbUploadOpen(false);
    setDbUploadSubmitting(false);
    setDbUploadError(null);
    setDbUploadSuccess("");
    setDbUploadFile(null);
    setDbUploadName("");
    setDbUploadNameError("");
    if (dbUploadInputRef.current) {
      dbUploadInputRef.current.value = "";
    }
  };

  const handleDbUploadFileChange = (event) => {
    const file = event.target.files?.[0] || null;
    setDbUploadFile(file);
    setDbUploadError(null);
    setDbUploadSuccess("");
    if (file?.name) {
      const fileStem = file.name.replace(/\.[^.]+$/, "");
      setDbUploadName(fileStem);
      setDbUploadNameError(getDbNameError(fileStem));
    } else {
      setDbUploadName("");
      setDbUploadNameError(getDbNameError(""));
    }
  };

  const handleDbUpload = async () => {
    if (!dbUploadFile) {
      setDbUploadError("CSV 파일을 선택해 주세요.");
      return;
    }
    if (!dbUploadFile.name.toLowerCase().endsWith(".csv")) {
      setDbUploadError("CSV 파일만 업로드할 수 있습니다.");
      return;
    }
    const trimmedName = dbUploadName.trim();
    const nameError = getDbNameError(trimmedName);
    if (nameError) {
      setDbUploadNameError(nameError);
      return;
    }
    setDbUploadSubmitting(true);
    setDbUploadError(null);
    setDbUploadSuccess("");
    try {
      const result = await onUploadDb({
        file: dbUploadFile,
        dbId: trimmedName || undefined,
      });
      const message =
        typeof result?.message === "string"
          ? result.message
          : "CSV 업로드를 완료했습니다.";
      const uploadedDbId =
        typeof result?.db_id === "string" ? result.db_id : "";
      const csvPath =
        typeof result?.path === "string" ? result.path : "";
      const preview = await parseCsvPreview(dbUploadFile, 3);
      const headers = Array.isArray(preview.headers) ? preview.headers : [];
      const rows = Array.isArray(preview.rows) ? preview.rows : [];
      setDbConvertPreview({ headers, rows });
      setDbColumnDescriptions(headers.map(() => ""));
      setDbTableDescription("");
      setDbConvertInfo({ dbId: uploadedDbId, csvPath });
      setDbConvertError(null);
      setDbConvertSuccess("");
      setIsDbConvertOpen(true);
      setDbUploadSuccess(
        uploadedDbId ? `${message} (DB ID: ${uploadedDbId})` : message,
      );
      closeDbUploadModal();
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "CSV 업로드 중 오류가 발생했습니다.";
      setDbUploadError(message);
    } finally {
      setDbUploadSubmitting(false);
    }
  };

  const closeDbConvertModal = () => {
    setIsDbConvertOpen(false);
    setDbConvertError(null);
    setDbConvertSubmitting(false);
    setDbConvertSuccess("");
    setDbConvertPreview({ headers: [], rows: [] });
    setDbColumnDescriptions([]);
    setDbTableDescription("");
    setDbConvertInfo({ dbId: "", csvPath: "" });
  };

  const handleDbColumnDescriptionChange = (index, value) => {
    setDbColumnDescriptions((prev) => {
      const next = [...prev];
      next[index] = value;
      return next;
    });
  };

  const handleDbConvert = async () => {
    if (!dbConvertInfo.dbId) {
      setDbConvertError("DB ID를 확인할 수 없습니다.");
      return;
    }
    if (dbConvertSubmitting) {
      return;
    }
    setDbConvertSubmitting(true);
    setDbConvertError(null);
    setDbConvertSuccess("");
    const descriptions = {
      table: dbTableDescription.trim(),
      column: dbConvertPreview.headers.map((_, index) =>
        (dbColumnDescriptions[index] || "").trim(),
      ),
    };
    try {
      const result = await onConvertDb({
        dbId: dbConvertInfo.dbId,
        csvPath: dbConvertInfo.csvPath,
        descriptions,
      });
      const message =
        typeof result?.message === "string"
          ? result.message
          : "SQLite 변환을 완료했습니다.";
      setDbConvertSuccess(message);
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "SQLite 변환 중 오류가 발생했습니다.";
      setDbConvertError(message);
    } finally {
      setDbConvertSubmitting(false);
    }
  };

  const handleGenerateContextQuestions = async () => {
    const trimmedTopic = contextTopic.trim();
    if (!trimmedTopic) {
      setContextFeedback({ type: "error", message: "정보 주제를 입력해 주세요." });
      return;
    }
    setContextGenerating(true);
    setContextFeedback(null);
    try {
      const questions = await onGenerateContextQuestions(trimmedTopic);
      if (!questions || !questions.length) {
        setContextFeedback({
          type: "error",
          message: "추가 질문을 생성하지 못했습니다.",
        });
        return;
      }
      setGeneratedQuestions(questions);
      setContextAnswers(Array(questions.length).fill(""));
      setContextStage("qa");
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "질문을 생성하는 중 오류가 발생했습니다.";
      setContextFeedback({ type: "error", message });
    } finally {
      setContextGenerating(false);
    }
  };

  const handleAnswerChange = (index, value) => {
    setContextAnswers((prev) => {
      const next = [...prev];
      next[index] = value;
      return next;
    });
  };

  const handleContextSave = async () => {
    const trimmedAnswers = contextAnswers.map((answer) => answer.trim());
    const hasEmpty = trimmedAnswers.some((answer) => !answer);
    if (hasEmpty) {
      setContextFeedback({
        type: "error",
        message: "모든 질문에 대한 답변을 입력해 주세요.",
      });
      return;
    }

    setContextSubmitting(true);
    setContextFeedback(null);
    try {
      await onSaveContextAnswers({
        topic: contextTopic,
        questions: generatedQuestions,
        answers: trimmedAnswers,
      });
      await loadContextNotes();
      setContextFeedback({ type: "success", message: "저장했습니다." });
      setContextStage("topic");
      setGeneratedQuestions([]);
      setContextAnswers([]);
      setContextFreeform("");
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "정보를 저장하는 중 문제가 발생했습니다.";
      setContextFeedback({ type: "error", message });
    } finally {
      setContextSubmitting(false);
    }
  };

  const resetContextFlow = () => {
    setContextStage("topic");
    setGeneratedQuestions([]);
    setContextAnswers([]);
    setContextFeedback(null);
    setContextGenerating(false);
    setContextFreeform("");
    setContextNotes([]);
    setContextNotesError(null);
    setContextNotesLoading(false);
  };

  const handleDirectContextSave = async () => {
    const trimmedContent = contextFreeform.trim();
    if (!trimmedContent) {
      setContextFeedback({
        type: "error",
        message: "바로 추가할 정보를 입력해 주세요.",
      });
      return;
    }
    setContextSubmitting(true);
    setContextFeedback(null);
    try {
      await onSaveFreeformContext({
        topic: contextTopic,
        content: trimmedContent,
      });
      await loadContextNotes();
      setContextFeedback({ type: "success", message: "바로 저장했습니다." });
      setContextFreeform("");
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "정보를 저장하는 중 문제가 발생했습니다.";
      setContextFeedback({ type: "error", message });
    } finally {
      setContextSubmitting(false);
    }
  };

  const handleDbSave = async () => {
    const trimmed = dbSelection.trim();
    if (!trimmed) {
      setDbError("변경할 DB를 선택해 주세요.");
      return;
    }
    setDbSubmitting(true);
    setDbError(null);
    try {
      await onChangeDb(trimmed);
      closeDbModal();
    } catch (error) {
      const message =
        error instanceof Error
          ? error.message
          : "DB 변경 중 문제가 발생했습니다.";
      setDbError(message);
    } finally {
      setDbSubmitting(false);
    }
  };

  const formatNoteDate = (value) => {
    if (!value) {
      return "";
    }
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) {
      return value;
    }
    return date.toLocaleString("ko-KR", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  };

  return (
    <form className="chat-input" onSubmit={handleSubmit} autoComplete="off">
      <div className="input-actions">
        <label className="sql-switch">
          <input
            type="checkbox"
            checked={sqlMode}
            onChange={(event) => onToggleSqlMode(event.target.checked)}
          />
          <span className="switch-track" aria-hidden="true">
            <span className="switch-thumb" />
          </span>
          <span className="switch-label">SQL 모드</span>
        </label>
        {canAddContext && (
          <>
            <button
              type="button"
              className="add-context-button"
              onClick={openContextModal}
              disabled={disabled || loading}
            >
              정보 추가하기
            </button>
            <button
              type="button"
              className="db-change-button"
              onClick={openDbModal}
              disabled={disabled || loading}
            >
              DB 변경하기
            </button>
            <span className="db-indicator">
              현재 DB: {currentDbId ? currentDbId : "미선택"}
            </span>
          </>
        )}
        {showSqlButton && (
          <>
            <button
              type="button"
              className="sql-generate-button"
              disabled={disabled || loading || sqlRunLoading || sqlTestLoading}
              onClick={onRunSql}
            >
              SQL 실행하기
            </button>

          </>
        )}
      </div>
      <div className="input-row">
        <div className="input-wrapper">
          <textarea
            ref={messageInputRef}
            id="message"
            name="message"
            className="input-field pipeline autoExpand"
            placeholder={placeholder}
            value={message}
            onChange={handleMessageChange}
            onKeyDown={handleMessageKeyDown}
            disabled={disabled || loading}
            rows={1}
            data-min-rows="1"
            spellCheck={false}
            required
          />
          <button
            type="submit"
            id="send-button"
            className="send-button"
            disabled={disabled || loading}
            aria-label="메시지 전송"
          >
            <svg
              aria-hidden="true"
              className="send-icon"
              viewBox="0 0 24 24"
              focusable="false"
            >
              <path
                d="M12 5v14m0-14 5 5m-5-5-5 5"
                fill="none"
                stroke="#000000"
                strokeWidth="2.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
            <span className="sr-only">{loading ? "전송 중" : "전송"}</span>
          </button>
        </div>
      </div>
      {isDbModalOpen && (
        <div
          className="context-modal-backdrop db-modal-backdrop"
          role="dialog"
          aria-modal="true"
        >
          <div className="context-modal db-modal">
            <div className="context-modal-header">
              <div className="context-modal-title">
                <h2>DB 변경하기</h2>
                <p className="db-modal-subtitle">
                  사용할 데이터베이스를 선택하세요.
                </p>
              </div>
              <button
                type="button"
                className="context-close-button"
                aria-label="DB 변경 닫기"
                onClick={closeDbModal}
              >
                ×
              </button>
            </div>
            <div className="db-modal-body">
              {dbLoading ? (
                <p className="db-modal-subtitle">DB 목록을 불러오는 중...</p>
              ) : dbList.length ? (
                <div className="db-dropdown" ref={dbDropdownRef}>
                  <button
                    type="button"
                    className="db-dropdown-trigger"
                    onClick={toggleDbDropdown}
                    onKeyDown={handleDbTriggerKeyDown}
                    ref={dbTriggerRef}
                    aria-haspopup="listbox"
                    aria-expanded={isDbDropdownOpen}
                    disabled={dbSubmitting}
                  >
                    <span>{dbSelection || "DB를 선택하세요"}</span>
                    <span aria-hidden="true" className="db-dropdown-icon">
                      ▾
                    </span>
                  </button>
                  {isDbDropdownOpen && (
                    <div
                      className="db-dropdown-menu"
                      role="listbox"
                      tabIndex={-1}
                      ref={dbListRef}
                      onKeyDown={handleDbDropdownKeyDown}
                      aria-activedescendant={
                        dbActiveIndex >= 0 ? `db-option-${dbActiveIndex}` : undefined
                      }
                    >
                      {dbList.map((dbId, index) => {
                        const isSelected = dbSelection === dbId;
                        const isActive = dbActiveIndex === index;
                        return (
                          <button
                            key={dbId}
                            type="button"
                            id={`db-option-${index}`}
                            role="option"
                            aria-selected={isSelected}
                            className={`db-dropdown-option${isActive ? " active" : ""}${isSelected ? " selected" : ""}`}
                            onClick={() => handleDbOptionSelect(dbId)}
                            onMouseEnter={() => setDbActiveIndex(index)}
                            disabled={dbSubmitting}
                          >
                            {dbId}
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              ) : (
                <p className="db-modal-subtitle">선택할 수 있는 DB가 없습니다.</p>
              )}
              {dbError && (
                <p className="db-modal-error" role="status">
                  {dbError}
                </p>
              )}
            </div>
            <div className="context-modal-actions">
              <button
                type="button"
                className="context-outline-button"
                onClick={openDbUploadModal}
                disabled={dbSubmitting}
              >
                DB 추가하기
              </button>
              <button
                type="button"
                className="context-secondary-button"
                onClick={closeDbModal}
                disabled={dbSubmitting}
              >
                취소
              </button>
              <button
                type="button"
                className="context-primary-button"
                onClick={handleDbSave}
                disabled={
                  dbSubmitting || dbLoading || !dbSelection || !dbList.length
                }
              >
                {dbSubmitting ? "변경 중..." : "변경"}
              </button>
            </div>
          </div>
        </div>
      )}
      {isDbUploadOpen && (
        <div
          className="context-modal-backdrop db-modal-backdrop"
          role="dialog"
          aria-modal="true"
        >
          <div className="context-modal db-upload-modal">
            <div className="context-modal-header">
              <div className="context-modal-title">
                <h2>DB 추가하기</h2>
                <p className="db-modal-subtitle">
                  CSV 파일을 업로드해 새로운 DB를 추가할 수 있습니다.
                </p>
              </div>
              <button
                type="button"
                className="context-close-button"
                aria-label="DB 추가 닫기"
                onClick={closeDbUploadModal}
                disabled={dbUploadSubmitting}
              >
                ×
              </button>
            </div>
            <div className="db-upload-body">
              <label className="db-upload-label" htmlFor="db-upload-input">
                CSV 파일
              </label>
              <input
                ref={dbUploadInputRef}
                id="db-upload-input"
                className="db-upload-input"
                type="file"
                accept=".csv,text/csv"
                onChange={handleDbUploadFileChange}
                disabled={dbUploadSubmitting}
              />
              {dbUploadFile && (
                <p className="db-upload-filename">{dbUploadFile.name}</p>
              )}
              <label className="db-upload-label" htmlFor="db-upload-name">
                DB 이름
              </label>
              <input
                id="db-upload-name"
                className="db-upload-text"
                type="text"
                value={dbUploadName}
                onChange={(event) => {
                  const value = event.target.value;
                  setDbUploadName(value);
                  setDbUploadNameError(getDbNameError(value));
                }}
                placeholder="업로드할 DB 이름을 입력하세요"
                disabled={dbUploadSubmitting}
              />
              {dbUploadNameError && (
                <p className="context-modal-feedback error" role="status">
                  {dbUploadNameError}
                </p>
              )}
              {dbUploadError && (
                <p className="context-modal-feedback error" role="status">
                  {dbUploadError}
                </p>
              )}
              {dbUploadSuccess && (
                <p className="context-modal-feedback success" role="status">
                  {dbUploadSuccess}
                </p>
              )}
            </div>
            <div className="context-modal-actions">
              <button
                type="button"
                className="context-secondary-button"
                onClick={closeDbUploadModal}
                disabled={dbUploadSubmitting}
              >
                닫기
              </button>
              <button
                type="button"
                className="context-primary-button"
                onClick={handleDbUpload}
                disabled={dbUploadSubmitting || !dbUploadFile || !!dbUploadNameError}
              >
                {dbUploadSubmitting ? "업로드 중..." : "업로드"}
              </button>
            </div>
          </div>
        </div>
      )}
      {isDbConvertOpen && (
        <div
          className="context-modal-backdrop db-modal-backdrop"
          role="dialog"
          aria-modal="true"
        >
          <div className="context-modal db-convert-modal">
            <div className="context-modal-header">
              <div className="context-modal-title">
                <h2>CSV → SQLite 변환</h2>
                <p className="db-modal-subtitle">
                  업로드한 CSV를 SQLite로 변환하기 전에 컬럼 설명을 추가하세요.
                </p>
              </div>
              <button
                type="button"
                className="context-close-button"
                aria-label="CSV 변환 닫기"
                onClick={closeDbConvertModal}
                disabled={dbConvertSubmitting}
              >
                ×
              </button>
            </div>
            <div className="db-convert-body">
              {dbConvertPreview.headers.length ? (
                <>
                  <div className="db-table-description">
                    <label className="db-upload-label" htmlFor="db-table-desc">
                      테이블 설명
                    </label>
                    <textarea
                      id="db-table-desc"
                      className="db-upload-text"
                      value={dbTableDescription}
                      onChange={(event) => setDbTableDescription(event.target.value)}
                      onInput={(event) => autoExpandTextarea(event.target)}
                      placeholder="테이블 전체 설명을 입력하세요"
                      disabled={dbConvertSubmitting}
                      rows={2}
                      data-min-rows="2"
                    />
                  </div>
                  <div className="db-preview-table">
                    <table className="result-table">
                      <thead>
                        <tr>
                          {dbConvertPreview.headers.map((header, index) => (
                            <th key={`${header}-${index}`}>{header || `열 ${index + 1}`}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        <tr className="db-column-inputs">
                          {dbConvertPreview.headers.map((header, index) => (
                            <td key={`desc-${index}`}>
                              <textarea
                                className="db-column-input"
                                value={dbColumnDescriptions[index] || ""}
                                onChange={(event) =>
                                  handleDbColumnDescriptionChange(index, event.target.value)
                                }
                                onInput={(event) => autoExpandTextarea(event.target)}
                                placeholder="컬럼 설명"
                                disabled={dbConvertSubmitting}
                                rows={1}
                                data-min-rows="1"
                              />
                            </td>
                          ))}
                        </tr>
                        {dbConvertPreview.rows.map((row, rowIndex) => (
                          <tr key={`row-${rowIndex}`}>
                            {dbConvertPreview.headers.map((_, colIndex) => (
                              <td key={`cell-${rowIndex}-${colIndex}`}>
                                {row[colIndex] ?? ""}
                              </td>
                            ))}
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </>
              ) : (
                <p className="db-modal-subtitle">
                  미리보기를 생성하지 못했습니다.
                </p>
              )}
              {dbConvertError && (
                <p className="context-modal-feedback error" role="status">
                  {dbConvertError}
                </p>
              )}
              {dbConvertSuccess && (
                <p className="context-modal-feedback success" role="status">
                  {dbConvertSuccess}
                </p>
              )}
            </div>
            <div className="context-modal-actions">
              <button
                type="button"
                className="context-secondary-button"
                onClick={closeDbConvertModal}
                disabled={dbConvertSubmitting}
              >
                닫기
              </button>
              <button
                type="button"
                className="context-primary-button"
                onClick={handleDbConvert}
                disabled={
                  dbConvertSubmitting ||
                  Boolean(dbConvertSuccess) ||
                  !dbConvertPreview.headers.length
                }
              >
                {dbConvertSubmitting ? "변환 중..." : "SQLite로 변환"}
              </button>
            </div>
          </div>
        </div>
      )}
      {isContextModalOpen && (
        <div className="context-modal-backdrop" role="dialog" aria-modal="true">
          <div className="context-modal">
            <div className="context-modal-header">
              <div className="context-modal-title">
                <h2>SQL 생성을 위한 추가 정보 입력</h2>
                <p className="context-modal-subtitle">
                  이 곳에 추가된 정보는 SQL 생성에만 이용됩니다.
                </p>
              </div>
              <button
                type="button"
                className="context-close-button"
                aria-label="추가 정보 입력 닫기"
                onClick={closeContextModal}
              >
                ×
              </button>
            </div>
            <div className="context-stage">
              <span
                className={`context-stage-pill ${contextStage === "topic" ? "active" : ""}`}
              >
                1. 주제 입력
              </span>
              <span
                className={`context-stage-pill ${contextStage === "qa" ? "active" : ""}`}
              >
                2. 질문 답변
              </span>
              <button
                type="button"
                className="context-stage-pill context-stage-button"
                onClick={loadContextNotes}
                disabled={contextNotesLoading}
              >
                {contextNotesLoading ? "갱신 중..." : "새로 고침"}
              </button>
            </div>
            <div className="context-notes-panel">
              <p className="context-notes-title">현재 추가된 정보</p>
              {contextNotesLoading && (
                <p className="context-notes-status">저장된 정보를 불러오는 중...</p>
              )}
              {contextNotesError && (
                <p className="context-modal-feedback error" role="status">
                  {contextNotesError}
                </p>
              )}
              {!contextNotesLoading && !contextNotesError && (
                <>
                  {contextNotes.length === 0 ? (
                    <p className="context-notes-status">아직 추가된 정보가 없습니다.</p>
                  ) : (
                    <div className="context-notes-list">
                      {contextNotes.map((note, index) => {
                        const noteKey = note?.created_at || `${note?.topic || "note"}-${index}`;
                        const questions = Array.isArray(note?.questions)
                          ? note.questions
                          : [];
                        const answers = Array.isArray(note?.answers) ? note.answers : [];
                        const pairs = questions.map((question, idx) => ({
                          question,
                          answer: answers[idx],
                        }));
                        return (
                          <div className="context-note-card" key={noteKey}>
                            <div className="context-note-header">
                              <strong>{note?.topic || `정보 ${index + 1}`}</strong>
                              {note?.created_at && (
                                <span>{formatNoteDate(note.created_at)}</span>
                              )}
                            </div>
                            {pairs.length > 0 ? (
                              <ul className="context-note-qa">
                                {pairs.map((pair, pairIdx) => (
                                  <li key={`${noteKey}-qa-${pairIdx}`}>
                                    <p className="context-note-question">
                                      Q{pairIdx + 1}. {pair.question}
                                    </p>
                                    {pair.answer && (
                                      <p className="context-note-answer">A. {pair.answer}</p>
                                    )}
                                  </li>
                                ))}
                              </ul>
                            ) : (
                              <p className="context-note-evidence">
                                {note?.evidence || "저장된 상세 내용이 없습니다."}
                              </p>
                            )}
                          </div>
                        );
                      })}
                    </div>
                  )}
                </>
              )}
            </div>
            {contextStage === "topic" && (
              <>
                <p className="context-modal-subtitle">
                  먼저 추가 정보를 얻고 싶은 주제를 입력하면 챗봇이 3개의 핵심 질문을
                  만들어 드립니다.
                </p>
                <textarea
                  rows={4}
                  className="context-modal-textarea"
                  placeholder="예) A 공정의 CYCLE TIME을 계산하고 싶어요."
                  value={contextTopic}
                  onChange={(event) => setContextTopic(event.target.value)}
                  disabled={contextGenerating || contextSubmitting}
                  spellCheck={false}
                />
                <p className="context-modal-subtitle subtle">
                  또는 아래 입력란에 바로 추가하고 싶은 정보를 적고 저장할 수 있어요.
                </p>
                <textarea
                  rows={4}
                  className="context-modal-textarea"
                  placeholder="바로 저장할 정보를 입력하세요."
                  value={contextFreeform}
                  onChange={(event) => setContextFreeform(event.target.value)}
                  disabled={contextSubmitting}
                  spellCheck={false}
                />
              </>
            )}
            {contextStage === "qa" && (
              <div className="context-question-list">
                <p className="context-modal-subtitle">
                  아래 질문에 답변하면 QA 형태로 정리되어 세션에 저장됩니다.
                </p>
                {generatedQuestions.map((question, index) => (
                  <div className="context-question-card" key={`question-${index}`}>
                    <p className="context-question-label">
                      Q{index + 1}. {question}
                    </p>
                    <textarea
                      rows={3}
                      className="context-answer-textarea"
                      placeholder="여기에 답변을 입력하세요."
                      value={contextAnswers[index] || ""}
                      onChange={(event) => handleAnswerChange(index, event.target.value)}
                      disabled={contextSubmitting}
                      spellCheck={false}
                    />
                  </div>
                ))}
              </div>
            )}
            {contextFeedback && (
              <p
                className={`context-modal-feedback ${contextFeedback.type === "error" ? "error" : "success"}`}
                role="status"
              >
                {contextFeedback.message}
              </p>
            )}
            <div className="context-modal-actions">
              {contextStage === "qa" && (
                <button
                  type="button"
                  className="context-secondary-button"
                  onClick={resetContextFlow}
                  disabled={contextSubmitting}
                >
                  처음으로
                </button>
              )}
              <button
                type="button"
                className="context-secondary-button"
                onClick={closeContextModal}
                disabled={contextSubmitting || contextGenerating}
              >
                닫기
              </button>
              {contextStage === "topic" ? (
                <>
                  <button
                    type="button"
                    className="context-secondary-button"
                    onClick={handleDirectContextSave}
                    disabled={contextSubmitting || !contextFreeform.trim()}
                  >
                    {contextSubmitting ? "저장 중..." : "바로 저장"}
                  </button>
                  <button
                    type="button"
                    className="context-primary-button"
                    onClick={handleGenerateContextQuestions}
                    disabled={contextGenerating || contextSubmitting}
                  >
                    {contextGenerating ? "생성 중..." : "질문 생성"}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="context-primary-button"
                  onClick={handleContextSave}
                  disabled={contextSubmitting}
                >
                  {contextSubmitting ? "저장 중..." : "저장"}
                </button>
              )}
            </div>
          </div>
        </div>
      )}
    </form>
  );
}
