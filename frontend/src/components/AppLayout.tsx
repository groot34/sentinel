import { Link, NavLink, Outlet } from "react-router-dom";

/** Application shell: header, primary navigation and page outlet. */
export function AppLayout() {
  return (
    <div className="app">
      <header className="app__header">
        <Link to="/" className="brand">
          <span className="brand__mark" aria-hidden="true" />
          <span className="brand__name">Sentinel</span>
          <span className="brand__tag">Incident Investigator</span>
        </Link>
        <nav className="nav" aria-label="Primary">
          <NavLink
            to="/"
            end
            className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}
          >
            Investigations
          </NavLink>
          <NavLink
            to="/new"
            className={({ isActive }) => (isActive ? "nav__link nav__link--active" : "nav__link")}
          >
            New investigation
          </NavLink>
        </nav>
      </header>
      <main className="app__main">
        <Outlet />
      </main>
      <footer className="app__footer">
        <span>Sentinel 2.0 — read-only diagnosis; fixes require human approval.</span>
      </footer>
    </div>
  );
}
