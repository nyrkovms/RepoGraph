import { Link, Route, Routes } from "react-router-dom";

import PackagePage from "./pages/PackagePage";
import ResolvePage from "./pages/ResolvePage";
import SearchPage from "./pages/SearchPage";
import UpdatesPage from "./pages/UpdatesPage";

export default function App() {
  return (
    <div className="app">
      <header className="app-header">
        <div>
          <h1>PackageHub MVP</h1>
          <p className="muted">PyPI catalog + sync + resolve</p>
        </div>
        <nav>
          <Link to="/">Search</Link>
          <Link to="/package/flask">Package</Link>
          <Link to="/resolve">Resolve</Link>
          <Link to="/updates">Updates</Link>
        </nav>
      </header>

      <main>
        <Routes>
          <Route path="/" element={<SearchPage />} />
          <Route path="/package/:name" element={<PackagePage />} />
          <Route path="/resolve" element={<ResolvePage />} />
          <Route path="/updates" element={<UpdatesPage />} />
        </Routes>
      </main>
    </div>
  );
}
