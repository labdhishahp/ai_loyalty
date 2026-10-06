/**
 * The four states every screen has to handle, in one place.
 *
 * Written once because they are easy to skip: a screen that only renders its
 * happy path looks finished in development, where the data always loads, and
 * breaks the first time a request fails in front of someone.
 */

export function Loading({ what }: { what: string }) {
  return (
    <div className="loading">
      <span className="spinner" /> Loading {what}…
    </div>
  );
}

export function Empty({ title, hint }: { title: string; hint?: string }) {
  return (
    <div className="empty">
      <div style={{ fontWeight: 600, color: "var(--text)" }}>{title}</div>
      {hint && <div style={{ marginTop: 6, fontSize: 14 }}>{hint}</div>}
    </div>
  );
}

export function Failed({ error, retry }: { error: string; retry?: () => void }) {
  return (
    <div className="error">
      <strong>Something went wrong</strong>
      {error}
      {retry && (
        <div style={{ marginTop: 10 }}>
          <button onClick={retry}>Try again</button>
        </div>
      )}
    </div>
  );
}

const TONE: Record<string, string> = {
  completed: "ok", approved: "ok", executed: "ok", pass: "ok",
  running: "busy", pending: "busy", draft: "busy",
  budget_exceeded: "warn", warn: "warn",
  failed: "bad", cancelled: "bad", rejected: "bad", fail: "bad",
};

export function Status({ value }: { value: string }) {
  return <span className={`pill ${TONE[value] ?? ""}`}>{value.replace("_", " ")}</span>;
}
