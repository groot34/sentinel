import type { ReactNode } from "react";

/** Centered spinner with a message, used while a request is in flight. */
export function LoadingState({ message = "Loading…" }: { message?: string }) {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" />
      <p>{message}</p>
    </div>
  );
}

interface ErrorStateProps {
  title?: string;
  message: string;
  onRetry?: () => void;
}

/** Error panel with an optional retry action. */
export function ErrorState({
  title = "Something went wrong",
  message,
  onRetry,
}: ErrorStateProps) {
  return (
    <div className="state state--error" role="alert">
      <h2>{title}</h2>
      <p>{message}</p>
      {onRetry && (
        <button type="button" className="button" onClick={onRetry}>
          Try again
        </button>
      )}
    </div>
  );
}

interface EmptyStateProps {
  title: string;
  message: string;
  action?: ReactNode;
}

/** Neutral placeholder for an empty collection. */
export function EmptyState({ title, message, action }: EmptyStateProps) {
  return (
    <div className="state state--empty">
      <h2>{title}</h2>
      <p>{message}</p>
      {action}
    </div>
  );
}
