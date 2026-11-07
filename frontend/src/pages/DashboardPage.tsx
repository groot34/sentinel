import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";

import { ApiError, getInvestigation, listInvestigations } from "../api/client";
import type { InvestigationDetail } from "../api/types";
import { StatusBadge } from "../components/StatusBadge";
import { EmptyState, ErrorState, LoadingState } from "../components/States";
import { formatTimestamp } from "../lib/format";

interface DashboardRow {
  id: string;
  detail: InvestigationDetail | null;
  error: string | null;
}

function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return "An unexpected error occurred.";
}

/**
 * Investigations dashboard.
 *
 * `GET /investigations` returns only IDs, so each ID's status and timestamps
 * are fetched from `GET /investigations/{id}` in parallel. A single failing
 * detail request degrades one row instead of failing the whole dashboard.
 */
export function DashboardPage() {
  const navigate = useNavigate();
  const [rows, setRows] = useState<DashboardRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const ids = await listInvestigations();
      const settled = await Promise.all(
        ids.map(async (id): Promise<DashboardRow> => {
          try {
            return { id, detail: await getInvestigation(id), error: null };
          } catch (detailError) {
            return { id, detail: null, error: describeError(detailError) };
          }
        }),
      );
      // Newest first: started_at is an ISO-8601 UTC string, so lexical order
      // is chronological. Rows without a detail fall back to their ID.
      settled.sort((a, b) => {
        const aTime = a.detail?.started_at ?? "";
        const bTime = b.detail?.started_at ?? "";
        return bTime.localeCompare(aTime);
      });
      setRows(settled);
    } catch (listError) {
      setError(describeError(listError));
      setRows(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <section className="page">
      <div className="page__head">
        <div>
          <h1>Investigations</h1>
          <p className="page__subtitle">
            Every investigation stored by the Sentinel backend.
          </p>
        </div>
        <Link to="/new" className="button button--primary">
          New investigation
        </Link>
      </div>

      {loading && <LoadingState message="Loading investigations…" />}

      {!loading && error && (
        <ErrorState
          title="Could not load investigations"
          message={error}
          onRetry={() => void load()}
        />
      )}

      {!loading && !error && rows && rows.length === 0 && (
        <EmptyState
          title="No investigations yet"
          message="Start by submitting an incident to investigate."
          action={
            <Link to="/new" className="button button--primary">
              New investigation
            </Link>
          }
        />
      )}

      {!loading && !error && rows && rows.length > 0 && (
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th scope="col">Incident</th>
                <th scope="col">Status</th>
                <th scope="col">Current stage</th>
                <th scope="col">Started</th>
                <th scope="col">Completed</th>
                <th scope="col" aria-label="Actions" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  className="table__row"
                  onClick={() => navigate(`/investigations/${encodeURIComponent(row.id)}`)}
                >
                  <td className="table__title">{row.id}</td>
                  <td>
                    {row.detail ? (
                      <StatusBadge status={row.detail.status} />
                    ) : (
                      <span className="muted" title={row.error ?? undefined}>
                        unavailable
                      </span>
                    )}
                  </td>
                  <td>{row.detail?.current_stage ?? "—"}</td>
                  <td>{formatTimestamp(row.detail?.started_at)}</td>
                  <td>{formatTimestamp(row.detail?.completed_at)}</td>
                  <td className="table__actions">
                    <Link
                      to={`/investigations/${encodeURIComponent(row.id)}`}
                      className="button button--ghost"
                      onClick={(event) => event.stopPropagation()}
                    >
                      Open
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
