import type { GitHubProvenance } from "../api/types";
import { formatTimestamp } from "../lib/format";
import { githubRepoLabel, shortCommitSha } from "../lib/github";

/** One labelled attribute in the provenance block. */
function ProvenanceMeta({ label, value }: { label: string; value: string }) {
  return (
    <div className="evidence__meta">
      <span className="evidence__meta-label">{label}</span>
      <span className="evidence__meta-value mono">{value}</span>
    </div>
  );
}

/**
 * GitHub provenance for a single evidence item.
 *
 * Rendered only when the backend attached a nested `github` block to the
 * evidence, so ordinary bundle evidence is never labelled as GitHub-derived.
 * The repository/commit/path/ref/retrieved-at metadata is shown compactly, and
 * the source link uses the backend's commit-pinned `source_url` and opens in a
 * new tab.
 */
export function GitHubProvenanceView({
  provenance,
}: {
  provenance: GitHubProvenance;
}) {
  const repo = githubRepoLabel(provenance.owner, provenance.repo);
  const shortSha = shortCommitSha(provenance.commit_sha);

  return (
    <div className="github-provenance">
      <div className="github-provenance__head">
        <span className="tag">GitHub</span>
        {repo && <span className="github-provenance__repo mono">{repo}</span>}
      </div>

      <div className="evidence__meta-grid">
        {provenance.path && (
          <ProvenanceMeta label="Path" value={provenance.path} />
        )}
        {provenance.commit_sha && (
          <div className="evidence__meta">
            <span className="evidence__meta-label">Commit</span>
            <span
              className="evidence__meta-value mono"
              title={provenance.commit_sha}
            >
              {shortSha}
            </span>
          </div>
        )}
        {provenance.ref && <ProvenanceMeta label="Reference" value={provenance.ref} />}
        {provenance.retrieved_at && (
          <ProvenanceMeta
            label="Retrieved"
            value={formatTimestamp(provenance.retrieved_at)}
          />
        )}
      </div>

      {provenance.source_url && (
        <p className="github-provenance__link">
          <a
            href={provenance.source_url}
            target="_blank"
            rel="noopener noreferrer"
          >
            View at commit{shortSha ? ` ${shortSha}` : ""}
            {provenance.line_anchor ? ` ${provenance.line_anchor}` : ""}
          </a>
        </p>
      )}
    </div>
  );
}
