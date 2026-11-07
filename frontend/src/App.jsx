import { Link, Navigate, Route, Routes, useLocation } from "react-router-dom";

import HomePage from "./pages/HomePage";
import PackagePage from "./pages/PackagePage";
import ResolvePage from "./pages/ResolvePage";
import SearchPage from "./pages/SearchPage";
import UpdatesPage from "./pages/UpdatesPage";

// `/` used to be the search page: a bookmark like `/?q=flask` goes on to `/search` with its query kept.
function HomeRoute() {
  const { search } = useLocation();
  return search ? <Navigate to={{ pathname: "/search", search }} replace /> : <HomePage />;
}

export default function App() {
  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>PackageHub</h1>
          <p className="muted">Package catalog: PyPI, npm, Maven, NuGet, Go, crates.io, RPM</p>
        </div>
        <nav>
          <Link to="/">Catalog</Link>
          <Link to="/updates">Updates</Link>
          <Link to="/resolve">Resolve</Link>
        </nav>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<HomeRoute />} />
          <Route path="/search" element={<SearchPage />} />
          <Route path="/package/:ecosystem/*" element={<PackagePage />} />
          <Route path="/package/:name" element={<PackagePage />} />
          <Route path="/resolve" element={<ResolvePage />} />
          <Route path="/updates" element={<UpdatesPage />} />
        </Routes>
      </main>
    </div>
  );
}
