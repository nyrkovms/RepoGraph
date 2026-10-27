import { Link, Route, Routes } from "react-router-dom";
import { useMemo, useState } from "react";

import PackagePage from "./pages/PackagePage";
import ResolvePage from "./pages/ResolvePage";
import SearchPage from "./pages/SearchPage";
import UpdatesPage from "./pages/UpdatesPage";
import { getAuthToken, setAuthToken } from "./lib/api";

export default function App() {
  const [tokenDraft, setTokenDraft] = useState(getAuthToken());
  const tokenPreview = useMemo(() => (tokenDraft ? `${tokenDraft.slice(0, 8)}...` : "none"), [tokenDraft]);

  const saveToken = () => {
    setAuthToken(tokenDraft);
    window.dispatchEvent(new Event("phub-token-updated"));
  };

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

      <section className="token-panel">
        <label>
          Bearer token
          <input
            value={tokenDraft}
            onChange={(e) => setTokenDraft(e.target.value)}
            placeholder="phub_viewer_dev"
          />
        </label>
        <button onClick={saveToken}>Use token</button>
        <span className="muted">Current: {tokenPreview}</span>
      </section>

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
