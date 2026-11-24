import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";

import {
  ApiError,
  connectGitHubRepository,
  disconnectGitHubConnection,
  listGitHubConnections,
  verifyGitHubConnection,
} from "../api/client";
import type { GitHubConnection } from "../api/types";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { formatTimestamp } from "../lib/format";
import {
  connectionStatusLabel,
  connectionStatusTone,
  githubRepoLabel,
} from "../lib/github";

/** Turn a failed GitHub request into a helpful, human-readable message. */
export function describeGitHubError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "github_integration_unavailable":
        return (
          `${error.message} The server must be configured with a GitHub ` +
          "credential before repositories can be connected."
        );
      case "duplicate_connection":
        return `${error.message} This repository is already connected.`;
      case "connection_not_found":
        return `${error.message} It may have been disconnected already.`;
      case "github_connection_unusable":
        return `${error.message} Verify the repository access and retry.`;
      case "network_error":
        return error.message;
      default:
        return `${error.message} (${error.code})`;
    }
  }
  if (error instanceof Error) return error.message;
  return "The GitHub request could not be completed.";
}

/** Return the first validation message for an owner/repo pair, or null. */
export function validateRepository(owner: string, repo: string): string | null {
  if (!owner.trim() || !repo.trim()) {
    return "Both the repository owner and name are required.";
  }
  return null;
}

/** Badge for a connection status, keeping connected/error/disabled distinct. */
function ConnectionStatusBadge({ status }: { status: string }) {
  return (
    <span className={`badge badge--${connectionStatusTone(status)}`} title={status}>
      {connectionStatusLabel(status)}
    </span>
  );
}

/**
 * GitHub connections manager.
 *
 * Lists the server-configured repository connections and lets an operator add,
 * verify and disconnect them through the existing read-only integration API.
 * The credential is configured server-side; the frontend never requests or
 * stores a GitHub token, and no credential reference is ever shown.
 */
export function ConnectionsPage() {
  const [connections, setConnections] = useState<GitHubConnection[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [owner, setOwner] = useState("");
  const [repo, setRepo] = useState("");
  const [label, setLabel] = useState("");
  const [defaultBranch, setDefaultBranch] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [busyId, setBusyId] = useState<string | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setLoadError(null);
    try {
      setConnections(await listGitHubConnections());
    } catch (error) {
      setLoadError(describeGitHubError(error));
      setConnections(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const validationError = useMemo(
    () => validateRepository(owner, repo),
    [owner, repo],
  );

  async function handleAdd(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setAddError(null);
    setNotice(null);
    if (validationError) {
      setAddError(validationError);
      return;
    }

    setAdding(true);
    try {
      const created = await connectGitHubRepository({
        owner: owner.trim(),
        repo: repo.trim(),
        ...(label.trim() ? { label: label.trim() } : {}),
        ...(defaultBranch.trim() ? { default_branch: defaultBranch.trim() } : {}),
      });
      setConnections((previous) => [
        created,
        ...(previous ?? []).filter(
          (c) => c.connection_id !== created.connection_id,
        ),
      ]);
      setNotice(`Connected ${created.owner}/${created.repo}.`);
      setOwner("");
      setRepo("");
      setLabel("");
      setDefaultBranch("");
    } catch (error) {
      setAddError(describeGitHubError(error));
    } finally {
      setAdding(false);
    }
  }

  async function handleVerify(connectionId: string) {
    setBusyId(connectionId);
    setActionError(null);
    setNotice(null);
    try {
      const response = await verifyGitHubConnection(connectionId);
      setConnections((previous) =>
        (previous ?? []).map((c) =>
          c.connection_id === response.connection.connection_id
            ? response.connection
            : c,
        ),
      );
      setNotice(response.message);
    } catch (error) {
      setActionError(describeGitHubError(error));
    } finally {
      setBusyId(null);
    }
  }

  async function handleDisconnect(connectionId: string) {
    setBusyId(connectionId);
    setActionError(null);
    setNotice(null);
    try {
      const response = await disconnectGitHubConnection(connectionId);
      setConnections((previous) =>
        (previous ?? []).filter(
          (c) => c.connection_id !== response.connection_id,
        ),
      );
      setConfirmId(null);
      setNotice(
        response.deleted
          ? `Disconnected ${response.connection_id}.`
          : `Connection ${response.connection_id} was already removed.`,
      );
    } catch (error) {
      setActionError(describeGitHubError(error));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="page">
      <div className="page__head">
        <div>
          <h1>GitHub connections</h1>
          <p className="page__subtitle">
            Read-only repository connections the pipeline can use to enrich an
            investigation. Credentials are configured on the server and are
            never shown here.
          </p>
        </div>
      </div>

      <form className="card form" onSubmit={handleAdd} noValidate>
        <h2>Connect a repository</h2>
        <div className="field-grid">
          <div className="field">
            <label htmlFor="gh_owner">Owner</label>
            <input
              id="gh_owner"
              name="gh_owner"
              type="text"
              autoComplete="off"
              placeholder="octocat"
              value={owner}
              onChange={(event) => setOwner(event.target.value)}
              disabled={adding}
            />
          </div>
          <div className="field">
            <label htmlFor="gh_repo">Repository</label>
            <input
              id="gh_repo"
              name="gh_repo"
              type="text"
              autoComplete="off"
              placeholder="hello-world"
              value={repo}
              onChange={(event) => setRepo(event.target.value)}
              disabled={adding}
            />
          </div>
        </div>
        <div className="field-grid">
          <div className="field">
            <label htmlFor="gh_label">Label (optional)</label>
            <input
              id="gh_label"
              name="gh_label"
              type="text"
              autoComplete="off"
              placeholder="Checkout service"
              value={label}
              onChange={(event) => setLabel(event.target.value)}
              disabled={adding}
            />
          </div>
          <div className="field">
            <label htmlFor="gh_branch">Default branch (optional)</label>
            <input
              id="gh_branch"
              name="gh_branch"
              type="text"
              autoComplete="off"
              placeholder="main"
              value={defaultBranch}
              onChange={(event) => setDefaultBranch(event.target.value)}
              disabled={adding}
            />
          </div>
        </div>

        {addError && (
          <div className="alert alert--error" role="alert">
            {addError}
          </div>
        )}

        <div className="form__actions">
          <button
            type="submit"
            className="button button--primary"
            disabled={adding}
          >
            {adding ? "Connecting…" : "Connect repository"}
          </button>
        </div>
      </form>

      {notice && (
        <div className="alert alert--info" role="status">
          {notice}
        </div>
      )}

      {actionError && (
        <div className="alert alert--error" role="alert">
          {actionError}
        </div>
      )}

      {loading && <LoadingState message="Loading GitHub connections…" />}

      {!loading && loadError && (
        <ErrorState
          title="Could not load GitHub connections"
          message={loadError}
          onRetry={() => void load()}
        />
      )}

      {!loading && !loadError && connections && connections.length === 0 && (
        <EmptyState
          title="No GitHub connections"
          message="Connect a repository above to enrich investigations with read-only code context."
        />
      )}

      {!loading && !loadError && connections && connections.length > 0 && (
        <ul className="cards">
          {connections.map((connection) => {
            const busy = busyId === connection.connection_id;
            return (
              <li key={connection.connection_id} className="card card--nested">
                <div className="card__head">
                  <span className="github__repo">
                    {githubRepoLabel(connection.owner, connection.repo)}
                  </span>
                  <ConnectionStatusBadge status={connection.status} />
                </div>
                <p className="card__body">{connection.label}</p>
                <div className="evidence__meta-grid">
                  <div className="evidence__meta">
                    <span className="evidence__meta-label">Default branch</span>
                    <span className="evidence__meta-value mono">
                      {connection.default_branch}
                    </span>
                  </div>
                  <div className="evidence__meta">
                    <span className="evidence__meta-label">Visibility</span>
                    <span className="evidence__meta-value mono">
                      {connection.visibility}
                    </span>
                  </div>
                  <div className="evidence__meta">
                    <span className="evidence__meta-label">Last verified</span>
                    <span className="evidence__meta-value">
                      {formatTimestamp(connection.last_verified_at)}
                    </span>
                  </div>
                </div>

                <div className="approval__actions">
                  <button
                    type="button"
                    className="button button--ghost"
                    onClick={() => void handleVerify(connection.connection_id)}
                    disabled={busy}
                  >
                    {busy && confirmId !== connection.connection_id
                      ? "Verifying…"
                      : "Verify"}
                  </button>
                  <button
                    type="button"
                    className="button button--danger"
                    onClick={() => setConfirmId(connection.connection_id)}
                    disabled={busy || confirmId === connection.connection_id}
                  >
                    Disconnect
                  </button>
                </div>

                {confirmId === connection.connection_id && (
                  <div className="approval__confirm">
                    <p className="approval__confirm-text">
                      Disconnect{" "}
                      <span className="mono">
                        {githubRepoLabel(connection.owner, connection.repo)}
                      </span>
                      ? Investigations already run are unaffected, but new runs
                      can no longer use this connection.
                    </p>
                    <div className="approval__actions">
                      <button
                        type="button"
                        className="button button--danger"
                        onClick={() =>
                          void handleDisconnect(connection.connection_id)
                        }
                        disabled={busy}
                      >
                        {busy ? "Disconnecting…" : "Confirm disconnect"}
                      </button>
                      <button
                        type="button"
                        className="button button--ghost"
                        onClick={() => setConfirmId(null)}
                        disabled={busy}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      <p className="muted">
        Need to run an investigation with a repository? Use{" "}
        <Link to="/new">New investigation</Link>.
      </p>
    </section>
  );
}
