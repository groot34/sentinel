import { useCallback, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import {
  ApiError,
  INCIDENT_ID_PATTERN,
  createInvestigation,
  listGitHubConnections,
} from "../api/client";
import type { GitHubConnection } from "../api/types";
import { githubRepoLabel } from "../lib/github";

/** Return the first validation message for an incident ID, or null when valid. */
export function validateIncidentId(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) return "An incident ID is required.";
  if (!INCIDENT_ID_PATTERN.test(trimmed)) {
    return (
      "Use 1–128 characters: letters, digits, hyphen or underscore, " +
      "starting with a letter or digit."
    );
  }
  return null;
}

/**
 * Validate the optional GitHub selection.
 *
 * GitHub is optional, but once it is switched on a connection is required: the
 * UI never silently drops back to bundle-only mode when the user asked for
 * repository context.
 */
export function validateGitHubSelection(
  enabled: boolean,
  connectionId: string,
): string | null {
  if (!enabled) return null;
  if (!connectionId.trim()) {
    return "Select a GitHub connection, or turn GitHub integration off.";
  }
  return null;
}

/** Turn a failed submission into a helpful, human-readable message. */
export function describeSubmitError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "incident_not_found":
        return (
          `${error.message} The incident bundle must already exist in the ` +
          "backend's incidents directory."
        );
      case "github_connection_unusable":
        return (
          `${error.message} Verify the connection on the GitHub connections ` +
          "page, or start the investigation without GitHub."
        );
      case "connection_not_found":
        return (
          `${error.message} It may have been disconnected; refresh the GitHub ` +
          "connections list."
        );
      case "github_integration_unavailable":
        return `${error.message} Start the investigation without GitHub instead.`;
      case "network_error":
        return error.message;
      default:
        return `${error.message} (${error.code})`;
    }
  }
  if (error instanceof Error) return error.message;
  return "The investigation could not be started.";
}

/**
 * Create Investigation form.
 *
 * Posts to `POST /investigations` with the backend's exact request shape and
 * `Prefer: respond-async`, so the run is accepted immediately and the workspace
 * can follow it over SSE. A successful run is navigated to by its ID.
 *
 * GitHub enrichment is optional: when switched on, an existing connection (and
 * an optional branch/tag/commit ref) is attached. The connections list is only
 * fetched once GitHub is enabled, so a bundle-only submission is unchanged.
 */
export function CreateInvestigationPage() {
  const navigate = useNavigate();
  const [incidentId, setIncidentId] = useState("");
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [githubEnabled, setGithubEnabled] = useState(false);
  const [connections, setConnections] = useState<GitHubConnection[] | null>(null);
  const [connectionsLoading, setConnectionsLoading] = useState(false);
  const [connectionsError, setConnectionsError] = useState<string | null>(null);
  const [selectedConnectionId, setSelectedConnectionId] = useState("");
  const [githubRef, setGithubRef] = useState("");

  const validationError = useMemo(() => validateIncidentId(incidentId), [incidentId]);
  const showValidationError = touched && validationError !== null;

  const githubValidationError = useMemo(
    () => validateGitHubSelection(githubEnabled, selectedConnectionId),
    [githubEnabled, selectedConnectionId],
  );

  const loadConnections = useCallback(async () => {
    setConnectionsLoading(true);
    setConnectionsError(null);
    try {
      const loaded = await listGitHubConnections();
      setConnections(loaded);
      // Only auto-select a single *usable* connection. Unusable connections
      // (error/disabled) are never auto-selected, and multiple usable
      // connections require an explicit choice from the operator.
      const connected = loaded.filter((c) => c.status === "connected");
      if (connected.length === 1) {
        setSelectedConnectionId((current) => current || connected[0].connection_id);
      }
    } catch (error) {
      setConnectionsError(
        error instanceof ApiError ? error.message : "Could not load connections.",
      );
      setConnections(null);
    } finally {
      setConnectionsLoading(false);
    }
  }, []);

  // Fetch the connections lazily — only once GitHub is switched on — so a
  // bundle-only investigation never issues a GitHub request.
  useEffect(() => {
    if (githubEnabled && connections === null && !connectionsLoading && !connectionsError) {
      void loadConnections();
    }
  }, [githubEnabled, connections, connectionsLoading, connectionsError, loadConnections]);

  const selectedConnection = useMemo(
    () =>
      connections?.find((c) => c.connection_id === selectedConnectionId) ?? null,
    [connections, selectedConnectionId],
  );

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTouched(true);
    setSubmitError(null);
    if (validationError || githubValidationError) return;

    setSubmitting(true);
    try {
      const accepted = await createInvestigation(incidentId.trim(), {
        ...(githubEnabled ? { githubConnectionId: selectedConnectionId } : {}),
        ...(githubEnabled && githubRef.trim() ? { githubRef: githubRef.trim() } : {}),
      });
      navigate(`/investigations/${encodeURIComponent(accepted.investigation_id)}`);
    } catch (error) {
      setSubmitError(describeSubmitError(error));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="page page--narrow">
      <div className="page__head">
        <div>
          <h1>New investigation</h1>
          <p className="page__subtitle">
            Submit an incident bundle for the Sentinel pipeline to investigate.
          </p>
        </div>
      </div>

      <form className="card form" onSubmit={handleSubmit} noValidate>
        <div className="field">
          <label htmlFor="incident_id">Incident ID</label>
          <input
            id="incident_id"
            name="incident_id"
            type="text"
            autoComplete="off"
            autoFocus
            placeholder="inc_01_n_plus_one_query"
            value={incidentId}
            aria-invalid={showValidationError}
            aria-describedby="incident_id_help"
            onChange={(event) => setIncidentId(event.target.value)}
            onBlur={() => setTouched(true)}
            disabled={submitting}
          />
          <p id="incident_id_help" className="field__help">
            The ID of an incident bundle that already exists in the backend's
            <code> incidents/</code> directory (for example{" "}
            <code>inc_01_n_plus_one_query</code>).
          </p>
          {showValidationError && (
            <p className="field__error" role="alert">
              {validationError}
            </p>
          )}
        </div>

        <div className="field field--check">
          <label htmlFor="github_enabled">
            <input
              id="github_enabled"
              name="github_enabled"
              type="checkbox"
              checked={githubEnabled}
              onChange={(event) => setGithubEnabled(event.target.checked)}
              disabled={submitting}
            />{" "}
            Enrich with a GitHub repository (optional)
          </label>
          <p className="field__help">
            Adds read-only repository context to the diagnosis. Requires a
            connection configured on the{" "}
            <Link to="/connections">GitHub connections</Link> page.
          </p>
        </div>

        {githubEnabled && (
          <>
            {connectionsLoading && (
              <p className="muted" role="status">
                Loading GitHub connections…
              </p>
            )}

            {connectionsError && (
              <div className="alert alert--error" role="alert">
                {connectionsError} GitHub enrichment cannot be used right now;
                turn it off to start a bundle-only investigation.
              </div>
            )}

            {!connectionsLoading && !connectionsError && connections && connections.length === 0 && (
              <div className="alert alert--info" role="status">
                No GitHub connections are configured. Add one on the{" "}
                <Link to="/connections">GitHub connections</Link> page, or turn
                GitHub integration off.
              </div>
            )}

            {!connectionsLoading && !connectionsError && connections && connections.length > 0 && (
              <>
                <div className="field">
                  <label htmlFor="github_connection">GitHub connection</label>
                  <select
                    id="github_connection"
                    name="github_connection"
                    className="input"
                    value={selectedConnectionId}
                    onChange={(event) => setSelectedConnectionId(event.target.value)}
                    disabled={submitting}
                  >
                    <option value="">Select a connection…</option>
                    {connections.map((connection) => (
                      <option
                        key={connection.connection_id}
                        value={connection.connection_id}
                        disabled={connection.status !== "connected"}
                      >
                        {githubRepoLabel(connection.owner, connection.repo)}
                        {connection.status !== "connected"
                          ? ` (${connection.status})`
                          : ""}
                      </option>
                    ))}
                  </select>
                </div>

                <div className="field">
                  <label htmlFor="github_ref">Reference (optional)</label>
                  <input
                    id="github_ref"
                    name="github_ref"
                    type="text"
                    autoComplete="off"
                    placeholder={selectedConnection?.default_branch ?? "main"}
                    value={githubRef}
                    aria-describedby="github_ref_help"
                    onChange={(event) => setGithubRef(event.target.value)}
                    disabled={submitting}
                  />
                  <p id="github_ref_help" className="field__help">
                    A branch, tag or commit SHA. Leave blank to use the
                    connection's default branch
                    {selectedConnection ? ` (${selectedConnection.default_branch})` : ""}.
                  </p>
                </div>

                {githubValidationError && (
                  <p className="field__error" role="alert">
                    {githubValidationError}
                  </p>
                )}
              </>
            )}
          </>
        )}

        {submitError && (
          <div className="alert alert--error" role="alert">
            {submitError}
          </div>
        )}

        <div className="form__actions">
          <button
            type="submit"
            className="button button--primary"
            disabled={submitting}
          >
            {submitting ? "Starting…" : "Start investigation"}
          </button>
          <button
            type="button"
            className="button button--ghost"
            onClick={() => navigate("/")}
            disabled={submitting}
          >
            Cancel
          </button>
        </div>
      </form>
    </section>
  );
}
