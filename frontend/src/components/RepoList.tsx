"use client";

import { useState, useCallback } from "react";
import { deleteRepo } from "@/lib/api";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import type { Repo } from "@/lib/types";

interface RepoListProps {
  repos: Repo[];
}

export function RepoList({ repos: initialRepos }: RepoListProps) {
  const [repos, setRepos] = useState(initialRepos);
  const [pendingDelete, setPendingDelete] = useState<Repo | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
          return (
            <div
              key={r.id}
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
