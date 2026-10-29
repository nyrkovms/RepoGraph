const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";

async function request(path, { method = "GET", body } = {}) {
  const headers = { "Content-Type": "application/json" };

  const response = await fetch(`${API_BASE_URL}${path}`, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  let payload = null;
  const text = await response.text();
  if (text) {
    try {
      payload = JSON.parse(text);
    } catch {
      payload = { raw: text };
    }
  }

  if (!response.ok) {
    const error = payload?.error || {
      code: "http_error",
      message: `HTTP ${response.status}`,
      details: payload,
    };
    throw error;
  }

  return payload;
}

export async function fetchPackages(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/api/v1/packages${suffix}`);
}

export async function searchPackages(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/api/v1/search${suffix}`);
}

// Package routes are addressed by ecosystem and native coordinates (`@angular/core`, `left-pad`, `flask`); each
// path segment is encoded, the `/` between scope and name stays (SPEC-MVP-API.md section 3.3).
export function packagePath(ecosystem, coordinates) {
  const encoded = String(coordinates).split("/").map(encodeURIComponent).join("/");
  return `/api/v1/packages/${encodeURIComponent(ecosystem)}/${encoded}/-`;
}

function withQuery(path, params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  return search.toString() ? `${path}?${search.toString()}` : path;
}

export async function fetchPackage(ecosystem, coordinates) {
  return request(`${packagePath(ecosystem, coordinates)}/`);
}

export async function fetchVersion(ecosystem, coordinates, version) {
  return request(`${packagePath(ecosystem, coordinates)}/versions/${encodeURIComponent(version)}`);
}

export async function fetchVersions(ecosystem, coordinates, params = {}) {
  return request(withQuery(`${packagePath(ecosystem, coordinates)}/versions`, params));
}

export async function fetchDependencies(ecosystem, coordinates, version, params = {}) {
  return request(
    withQuery(`${packagePath(ecosystem, coordinates)}/versions/${encodeURIComponent(version)}/dependencies`, params)
  );
}

export async function fetchVulnerabilities(ecosystem, coordinates) {
  return request(`${packagePath(ecosystem, coordinates)}/vulnerabilities`);
}

export async function resolveDependencies(body) {
  return request(`/api/v1/resolve`, { method: "POST", body });
}

export async function fetchUpdatesSummary(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/api/v1/updates/summary${suffix}`);
}

export async function fetchUpdates(params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/api/v1/updates${suffix}`);
}
