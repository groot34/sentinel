import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";

import { AppLayout } from "./components/AppLayout";
import { CreateInvestigationPage } from "./pages/CreateInvestigationPage";
import { DashboardPage } from "./pages/DashboardPage";
import { WorkspacePage } from "./pages/WorkspacePage";

/** Application routes for the three connected experiences. */
export function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="new" element={<CreateInvestigationPage />} />
          <Route path="investigations/:id" element={<WorkspacePage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
