"use client";

import { useState, useCallback } from "react";
import { deleteRepo, updatePatrolSettings, triggerPatrol } from "@/lib/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { Repo } from "@/lib/types";

interface RepoListProps {
  repos: Repo[];
}

function PatrolPanel({ repo }: { repo: Repo }) {
  const [enabled, setEnabled] = useState(!!repo.patrol_enabled);
  const [intervalHours, setIntervalHours] = useState(repo.patrol_interval_hours ?? 12);
  const [maxIssues, setMaxIssues] = useState(repo.patrol_max_issues ?? 5);
  const [windowHours, setWindowHours] = useState(repo.patrol_window_hours ?? 12);
  const [saving, setSaving] = useState(false);
  const [triggering, setTriggering] = useState(false);
  const [saved, setSaved] = useState(false);
  const [triggerMsg, setTriggerMsg] = useState<{ text: string; ok: boolean } | null>(null);

  const handleSave = useCallback(async () => {
    setSaving(true);
    setSaved(false);
    const ok = await updatePatrolSettings(repo.id, {
      patrol_enabled: enabled,
      patrol_interval_hours: intervalHours,
      patrol_max_issues: maxIssues,
      patrol_window_hours: windowHours,
    });
    setSaving(false);
    if (ok) setSaved(true);
  }, [repo.id, enabled, intervalHours, maxIssues, windowHours]);

  const handleTrigger = useCallback(async () => {
    setTriggering(true);
    setTriggerMsg(null);
    const result = await triggerPatrol(repo.id);
    setTriggering(false);
    if (!result) {
      setTriggerMsg({ text: "Network error", ok: false });
    } else if ("error" in result) {
      setTriggerMsg({ text: `Failed: ${result.error}`, ok: false });
    } else {
      setTriggerMsg({ text: `Triggered (activity ${result.activity_id.slice(0, 8)})`, ok: true });
    }
  }, [repo.id]);

  return (
    <div className="mt-2 ml-5 bg-black/20 rounded-lg p-3 text-xs border border-white/5">
      <div className="flex items-center gap-3 mb-2">
        <label className="flex items-center gap-2 cursor-pointer select-none">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
            className="w-3.5 h-3.5 accent-emerald-400"
          />
          <span className="text-gray-300 font-medium">Enable Patrol</span>
        </label>
      </div>

      {enabled && (
        <div className="grid grid-cols-3 gap-2 mb-2">
          <label className="flex flex-col gap-1">
            <span className="text-gray-500">Interval (hrs)</span>
            <input
              type="number"
              min={1}
              max={168}
              value={intervalHours}
              onChange={(e) => setIntervalHours(Number(e.target.value))}
              className="bg-white/5 border border-white/10 rounded px-2 py-1 text-gray-300 w-full"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-gray-500">Max issues</span>
            <input
              type="number"
              min={1}
              max={20}
              value={maxIssues}
              onChange={(e) => setMaxIssues(Number(e.target.value))}
              className="bg-white/5 border border-white/10 rounded px-2 py-1 text-gray-300 w-full"
            />
          </label>
          <label className="flex flex-col gap-1">
            <span className="text-gray-500">Window (hrs)</span>
            <input
              type="number"
              min={1}
              max={168}
              value={windowHours}
              onChange={(e) => setWindowHours(Number(e.target.value))}
              className="bg-white/5 border border-white/10 rounded px-2 py-1 text-gray-300 w-full"
            />
          </label>
        </div>
      )}

      <div className="flex items-center gap-2 flex-wrap">
        <button
          onClick={handleSave}
          disabled={saving}
          className="px-2.5 py-1 rounded bg-white/10 hover:bg-white/15 text-gray-300 transition-colors disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save"}
        </button>
        <button
          onClick={handleTrigger}
          disabled={triggering}
          className="px-2.5 py-1 rounded bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 transition-colors disabled:opacity-50"
        >
          {triggering ? "Triggering…" : "Manual Trigger"}
        </button>
        {saved && <span className="text-emerald-400">Saved ✓</span>}
        {triggerMsg && (
          <span className={triggerMsg.ok ? "text-emerald-400" : "text-red-400"}>
            {triggerMsg.text}
          </span>
        )}
      </div>
    </div>
  );
}

export function RepoList({ repos: initialRepos }: RepoListProps) {
  const [repos, setRepos] = useState(initialRepos);
  const [pendingDelete, setPendingDelete] = useState<Repo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandedPatrol, setExpandedPatrol] = useState<string | null>(null);

  const handleConfirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    setError(null);
    console.log("[CatoCode] Deleting repo:", pendingDelete.id, pendingDelete.repo_url);
    const ok = await deleteRepo(pendingDelete.id);
    console.log("[CatoCode] Delete result:", ok);
    if (ok) {
      setRepos((prev) => prev.filter((r) => r.id !== pendingDelete.id));
      setPendingDelete(null);
    } else {
      setError("Failed to delete. Check if the backend is running.");
    }
    setDeleting(false);
  }, [pendingDelete]);

  const handleCancel = useCallback(() => {
    if (!deleting) {
      setPendingDelete(null);
      setError(null);
    }
  }, [deleting]);

  if (!repos.length) {
    return (
      <div className="flex flex-col items-center justify-center py-8 text-gray-600">
        <span className="text-2xl mb-2">📭</span>
        <p className="text-sm">No repositories yet.</p>
      </div>
    );
  }

  return (
    <>
      <div className="space-y-1">
        {repos.map((r) => {
          const shortName = r.repo_url.replace("https://github.com/", "");
          const patrolExpanded = expandedPatrol === r.id;
          return (
            <div key={r.id} className="py-1">
              <div
                className="flex items-center gap-3 py-2.5 px-3 -mx-3 rounded-lg hover:bg-white/[0.02] transition-colors text-sm group"
              >
                <span
                  className={`w-2 h-2 rounded-full flex-shrink-0 ${
                    r.watch ? "bg-emerald-400" : "bg-gray-600"
                  }`}
                />
                <a
                  href={r.repo_url}
                  target="_blank"
                  rel="noreferrer"
                  className="text-gray-300 hover:text-white truncate transition-colors font-medium"
                >
                  {shortName}
                </a>
                <span
                  className={`ml-auto text-xs px-2 py-0.5 rounded-full flex-shrink-0 ${
                    r.watch
                      ? "text-emerald-400 bg-emerald-400/10"
                      : "text-gray-500 bg-gray-500/10"
                  }`}
                >
                  {r.watch ? "watching" : "paused"}
                </span>
                {/* Patrol toggle button */}
                <button
                  onClick={() => setExpandedPatrol(patrolExpanded ? null : r.id)}
                  className={`opacity-0 group-hover:opacity-100 text-xs px-2 py-0.5 rounded transition-all flex-shrink-0 ${
                    r.patrol_enabled
                      ? "text-violet-400 bg-violet-400/10 hover:bg-violet-400/20"
                      : "text-gray-500 bg-gray-500/10 hover:bg-gray-500/20"
                  }`}
                  title="Patrol settings"
                >
                  {r.patrol_enabled ? "patrol on" : "patrol"}
                </button>
                <button
                  onClick={() => setPendingDelete(r)}
                  className="opacity-0 group-hover:opacity-100 text-gray-600 hover:text-red-400 transition-all flex-shrink-0 p-1 rounded hover:bg-red-400/10"
                  title="Remove repository"
                >
                  <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                    <path strokeLinecap="round" strokeLinejoin="round" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16" />
                  </svg>
                </button>
              </div>
              {patrolExpanded && <PatrolPanel repo={r} />}
            </div>
          );
        })}
      </div>

      <ConfirmDialog
        open={!!pendingDelete}
        title="Remove Repository"
        message={
          pendingDelete
            ? `Stop watching ${pendingDelete.repo_url.replace("https://github.com/", "")}? This will stop all automated reviews, issue analysis, and patrols for this repo.`
            : ""
        }
        confirmLabel="Remove"
        onConfirm={handleConfirmDelete}
        onCancel={handleCancel}
        loading={deleting}
      />

      {error && (
        <div className="mt-2 text-xs text-red-400 bg-red-400/10 rounded-lg px-3 py-2">
          {error}
        </div>
      )}
    </>
  );
}
