const API_BASE_URL = import.meta.env.VITE_API_BASE_URL || "http://localhost:8000";
const DEFAULT_TOKEN = import.meta.env.VITE_API_TOKEN || "phub_viewer_dev";

export function getAuthToken() {
  return localStorage.getItem("phub_token") || DEFAULT_TOKEN;
}

export function setAuthToken(token) {
  if (!token) {
    localStorage.removeItem("phub_token");
    return;
  }
  localStorage.setItem("phub_token", token.trim());
}

async function request(path, { method = "GET", body } = {}) {
  const headers = {
    "Content-Type": "application/json",
    Authorization: `Bearer ${getAuthToken()}`,
  };

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

export async function fetchPackage(name) {
  return request(`/api/v1/packages/pypi/${encodeURIComponent(name)}`);
}

export async function fetchVersions(name, params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(`/api/v1/packages/pypi/${encodeURIComponent(name)}/versions${suffix}`);
}

export async function fetchDependencies(name, version, params = {}) {
  const search = new URLSearchParams();
  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.set(key, String(value));
    }
  });
  const suffix = search.toString() ? `?${search.toString()}` : "";
  return request(
    `/api/v1/packages/pypi/${encodeURIComponent(name)}/versions/${encodeURIComponent(version)}/dependencies${suffix}`
  );
}

export async function fetchVulnerabilities(name) {
  return request(`/api/v1/packages/pypi/${encodeURIComponent(name)}/vulnerabilities`);
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
