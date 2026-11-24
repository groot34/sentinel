import type { GitHubAssociation, GitHubContext } from "../api/types";
import { formatTimestamp } from "../lib/format";
import {
  githubContextStatusLabel,
  githubContextStatusTone,
  githubRepoLabel,
  shortCommitSha,
} from "../lib/github";

interface GitHubContextPanelProps {
  context?: GitHubContext | null;
  association?: GitHubAssociation | null;
}

/** One labelled fact in the context panel. */
function ContextFact({ label, value }: { label: string; value: string }) {
  return (
    <div className="fact">
      <span className="fact__label">{label}</span>
      <span className="fact__value mono">{value}</span>
    </div>
  );
}

/**
 * Compact GitHub repository context for an investigation.
 *
 * Renders only when the investigation carries `github_context` or
 * `github_association` metadata, so a bundle-only (or legacy) investigation
 * shows no empty GitHub panel. The retrieval `status` distinguishes a
 * successful, partial and failed retrieval; for a partial/failed run the safe
 * error and any skipped files are shown.
 */
export function GitHubContextPanel({
  context,
  association,
}: GitHubContextPanelProps) {
  if (!context && !association) return null;

  const repo = githubRepoLabel(context?.owner, context?.repo);
  const requestedRef = context?.requested_ref ?? association?.ref ?? null;
  const shortSha = shortCommitSha(context?.commit_sha);
  const status = context?.status ?? null;
  // Tolerate an absent/null `skipped` list so a partial payload never crashes.
  const skipped = context?.skipped ?? [];

  return (
    <section className="panel" id="github-context">
      <div className="panel__head">
        <h2>GitHub repository context</h2>
        {status && (
          <span
            className={`badge badge--${githubContextStatusTone(status)}`}
            title={status}
          >
            {githubContextStatusLabel(status)}
          </span>
        )}
      </div>

      <div className="facts">
        {repo && <ContextFact label="Repository" value={repo} />}
        {requestedRef && <ContextFact label="Requested ref" value={requestedRef} />}
        {context?.commit_sha && (
          <div className="fact">
            <span className="fact__label">Resolved commit</span>
            <span className="fact__value mono" title={context.commit_sha}>
              {shortSha}
            </span>
          </div>
        )}
        {typeof context?.retrieved_file_count === "number" && (
          <ContextFact
            label="Files retrieved"
            value={String(context.retrieved_file_count)}
          />
        )}
        {typeof context?.evidence_count === "number" && (
          <ContextFact
            label="Evidence items"
            value={String(context.evidence_count)}
          />
        )}
        {context?.retrieved_at && (
          <ContextFact
            label="Retrieved"
            value={formatTimestamp(context.retrieved_at)}
          />
        )}
      </div>

      {context?.truncated && (
        <div className="alert alert--info" role="status">
          Retrieval was truncated
          {context.truncation_reason ? `: ${context.truncation_reason}` : "."}
        </div>
      )}

      {context?.error?.message && (
        <div className="alert alert--error" role="alert">
          <strong>{context.error.reason ?? "GitHub retrieval error"}:</strong>{" "}
          {context.error.message}
        </div>
      )}

      {skipped.length > 0 && (
        <div className="github-context__skipped">
          <h3>Skipped files ({skipped.length})</h3>
          <ul className="bullets">
            {skipped.map((file, index) => (
              <li key={`${file.path ?? "file"}-${index}`}>
                <span className="mono">{file.path ?? "unknown"}</span>
                {file.reason ? ` — ${file.reason}` : ""}
                {file.detail ? ` (${file.detail})` : ""}
              </li>
            ))}
          </ul>
        </div>
      )}

      {!context && association && (
        <p className="muted">
          This investigation is associated with a GitHub connection, but no
          retrieval context was captured.
        </p>
      )}
    </section>
  );
}
