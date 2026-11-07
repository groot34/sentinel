import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, INCIDENT_ID_PATTERN, createInvestigation } from "../api/client";

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

/** Turn a failed submission into a helpful, human-readable message. */
export function describeSubmitError(error: unknown): string {
  if (error instanceof ApiError) {
    switch (error.code) {
      case "incident_not_found":
        return (
          `${error.message} The incident bundle must already exist in the ` +
          "backend's incidents directory."
        );
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
 * Posts to `POST /investigations` with the backend's exact request shape
 * (`{"incident_id": "..."}`) and `Prefer: respond-async`, so the run is
 * accepted immediately and the workspace can follow it over SSE. On success
 * the user is navigated to the created investigation.
 */
export function CreateInvestigationPage() {
  const navigate = useNavigate();
  const [incidentId, setIncidentId] = useState("");
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const validationError = useMemo(() => validateIncidentId(incidentId), [incidentId]);
  const showValidationError = touched && validationError !== null;

  async function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTouched(true);
    setSubmitError(null);
    if (validationError) return;

    setSubmitting(true);
    try {
      const accepted = await createInvestigation(incidentId.trim());
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
