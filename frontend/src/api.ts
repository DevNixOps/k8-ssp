export type Me = { name: string; role: string; namespaces: string[] };

let token: string | null = sessionStorage.getItem("token");

export const getToken = () => token;
export const setToken = (t: string | null) => {
  token = t;
  if (t) sessionStorage.setItem("token", t);
  else sessionStorage.removeItem("token");
};

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api${path}`, {
    ...init,
    headers: { ...(init.headers || {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
    } catch {}
    throw new ApiError(res.status, detail);
  }
  return res.json();
}

export async function login(username: string, password: string) {
  const r = await api<{ access_token: string }>("/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({ username, password }),
  });
  setToken(r.access_token);
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
