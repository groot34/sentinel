-- 006_github_integrations_case_insensitive_unique
--
-- GitHub repository owner and repository names are case-insensitive, so a
-- given repository may be connected at most once regardless of casing
-- (e.g. "groot34/Sentinel" and "groot34/sentinel" denote the same repository).
--
-- Migration 005 created a case-sensitive unique index on (owner, repo). This
-- migration replaces it with a unique expression index over
-- (lower(owner), lower(repo)), preserving the index name so the existing schema
-- verification (EXPECTED_TABLES) continues to match.
--
-- Data safety:
--   * No existing row is modified, deleted or rewritten.
--   * Record casing is preserved: only the index expression is case-folded.
--   * If case-variant duplicates already exist the migration aborts with an
--     actionable error rather than failing obscurely on index creation or
--     silently discarding data. Resolve the conflict manually and re-run.

DROP INDEX IF EXISTS idx_github_integrations_owner_repo;

DO $$
DECLARE
    conflict_groups integer;
BEGIN
    SELECT count(*) INTO conflict_groups
    FROM (
        SELECT lower(owner) AS l_owner, lower(repo) AS l_repo
        FROM github_integrations
        GROUP BY lower(owner), lower(repo)
        HAVING count(*) > 1
    ) AS conflicts;

    IF conflict_groups > 0 THEN
        RAISE EXCEPTION
            'Cannot apply 006_github_integrations_case_insensitive_unique: % (owner, repo) case-variant group(s) already exist in github_integrations. Resolve these duplicate connections manually before re-running the migration.',
            conflict_groups;
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_github_integrations_owner_repo
    ON github_integrations (lower(owner), lower(repo));
