import type { ReactNode } from "react";

/** A load that failed, said once and the same way on every screen: the
 *  existing .notice.error card with its Retry pill (globals.css). role=alert,
 *  because it replaces content the reader was waiting for. Leave out
 *  `onRetry` when retrying would only meet the same refusal (a demo 429). */
export default function ErrorNotice({
  message,
  onRetry,
  retryLabel = "Retry",
}: {
  message: ReactNode;
  onRetry?: () => void;
  retryLabel?: string;
}) {
  return (
    <div className="notice error" role="alert">
      <span>{message}</span>
      {onRetry ? (
        <button type="button" className="notice-btn" onClick={onRetry}>
          {retryLabel}
        </button>
      ) : null}
    </div>
  );
}
