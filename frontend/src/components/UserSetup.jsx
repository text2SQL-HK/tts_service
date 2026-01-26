export default function UserSetup({
  pendingUserId,
  onUserIdChange,
  onSubmit,
  statusMessage,
  errorMessage,
  disabled,
  onReset = undefined,
}) {
  const handleSubmit = (event) => {
    event.preventDefault();
    if (disabled) {
      if (typeof onReset === "function") {
        onReset();
      }
    } else if (!errorMessage) {
      onSubmit();
    }
  };

  const displayMessage = errorMessage || statusMessage;
  const statusClassName = `user-status${errorMessage ? " user-status--error" : ""
    }`;

  return (
    <section className="user-setup">
      <form onSubmit={handleSubmit} autoComplete="off">
        <label htmlFor="user-id">사용자 ID</label>
        <div className="user-input-group">
          <input
            id="user-id"
            type="text"
            placeholder="예: alice123"
            value={pendingUserId}
            onChange={(event) => onUserIdChange(event.target.value)}
            disabled={disabled}
            required
          />
          <button type="submit" disabled={!disabled && Boolean(errorMessage)}>
            {disabled ? "변경" : "시작"}
          </button>
        </div>
      </form>
      <p className={statusClassName}>{displayMessage}</p>
    </section>
  );
}


