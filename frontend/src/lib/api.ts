import type { User, Stats, Repo, Activity } from "./types";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

/**
 * Get cookie header for server-side requests.
 * In server components, browser cookies aren't sent automatically,
 * so we need to forward them from the incoming request.
 */
async function getCookieHeader(): Promise<string> {
  if (typeof window !== "undefined") {
    // Client-side: browser handles cookies automatically
    return "";
  }
  try {
    // Server-side: forward cookies from incoming request
    const { cookies } = await import("next/headers");
    const cookieStore = await cookies();
    return cookieStore
      .getAll()
      .map((c) => `${c.name}=${c.value}`)
      .join("; ");
  } catch {
    return "";
  }
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T | null> {
  try {
    const cookieHeader = await getCookieHeader();
    const headers: Record<string, string> = {
      ...(init?.headers as Record<string, string>),
    };
    if (cookieHeader) {
      headers["Cookie"] = cookieHeader;
    }

    const res = await fetch(`${API_URL}${path}`, {
      credentials: "include", // for client-side
      cache: "no-store", // always fresh data in server components
      ...init,
      headers,
    });
    if (res.status === 401) return null;
    if (!res.ok) throw new Error(`API error ${res.status}: ${path}`);
    return res.json() as Promise<T>;
  } catch {
    return null;
  }
}

export async function getMe(): Promise<User | null> {
  return apiFetch<User>("/api/me");
}

export async function getStats(): Promise<Stats | null> {
  return apiFetch<Stats>("/api/stats");
}

export async function getRepos(): Promise<Repo[] | null> {
  return apiFetch<Repo[]>("/api/repos");
}

export async function getActivities(): Promise<Activity[] | null> {
  return apiFetch<Activity[]>("/api/activities");
}

export async function getInstallUrl(): Promise<string | null> {
  const data = await apiFetch<{ url: string }>("/api/install-url");
  return data?.url ?? null;
}

export async function logout(): Promise<void> {
  try {
    await fetch(`${API_URL}/auth/logout`, {
      method: "POST",
      credentials: "include",
    });
  } catch {
    // Ignore network/CORS errors — still clear local state
  }
}
