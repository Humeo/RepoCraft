import type { User, Stats, Repo, Activity, ActivityLog, PatrolStatus } from "./types";

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

export async function deleteRepo(repoId: string): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/api/repos/${repoId}`, {
      method: "DELETE",
      credentials: "include",
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function getActivity(activityId: string): Promise<Activity | null> {
  return apiFetch<Activity>(`/api/activities/${activityId}`);
}

export async function getActivityLogs(activityId: string): Promise<ActivityLog[] | null> {
  return apiFetch<ActivityLog[]>(`/api/activities/${activityId}/logs`);
}

export function getLogStreamUrl(activityId: string): string {
  return `${API_URL}/api/activities/${activityId}/logs/stream`;
}

export async function getPatrolStatus(repoId: string): Promise<PatrolStatus | null> {
  return apiFetch<PatrolStatus>(`/api/repos/${repoId}/patrol/status`);
}

export async function updatePatrolSettings(
  repoId: string,
  settings: {
    patrol_enabled: boolean;
    patrol_interval_hours: number;
    patrol_max_issues: number;
    patrol_window_hours: number;
  }
): Promise<boolean> {
  try {
    const res = await fetch(`${API_URL}/api/repos/${repoId}/patrol`, {
      method: "PATCH",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(settings),
    });
    return res.ok;
  } catch {
    return false;
  }
}

export async function triggerPatrol(
  repoId: string
): Promise<{ activity_id: string } | { error: string } | null> {
  try {
    const res = await fetch(`${API_URL}/api/repos/${repoId}/patrol/trigger`, {
      method: "POST",
      credentials: "include",
    });
    if (!res.ok) {
      try {
        const body = await res.json();
        return { error: body.detail ?? `HTTP ${res.status}` };
      } catch {
        return { error: `HTTP ${res.status}` };
      }
    }
    return res.json();
  } catch {
    return { error: "Network error" };
  }
}

export { API_URL };
