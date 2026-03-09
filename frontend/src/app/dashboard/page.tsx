import { getStats, getRepos, getActivities } from "@/lib/api";
import { LiveDashboard } from "@/components/LiveDashboard";
import Link from "next/link";

export default async function DashboardPage() {
  const [stats, repos, activities] = await Promise.all([
    getStats(),
    getRepos(),
    getActivities(),
  ]);

  const noRepos = !repos || repos.length === 0;

  return (
    <div className="space-y-6">
      {/* Install CTA */}
      {noRepos && (
        <div className="relative overflow-hidden rounded-xl border border-accent/20 bg-gradient-to-r from-accent/10 via-surface-1 to-purple-600/10 p-6 text-center">
          <div className="absolute inset-0 bg-gradient-to-r from-accent/5 to-purple-600/5" />
          <div className="relative">
            <div className="inline-flex items-center justify-center w-12 h-12 rounded-full bg-accent/20 mb-4">
              <span className="text-xl">🔗</span>
            </div>
            <p className="text-gray-300 mb-4 text-sm">
              No repositories watched yet. Install the GitHub App to get started.
            </p>
            <Link
              href="/install"
              className="inline-flex items-center gap-2 bg-accent hover:bg-accent-light text-white font-semibold px-6 py-2.5 rounded-lg transition-all duration-200 hover:scale-[1.02] active:scale-[0.98] glow-accent"
            >
              Install GitHub App
              <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M13 7l5 5m0 0l-5 5m5-5H6" />
              </svg>
            </Link>
          </div>
        </div>
      )}

      {/* Live-updating stats + activities */}
      <LiveDashboard
        initialStats={stats}
        initialActivities={stats?.recent_activities ?? activities ?? []}
        initialRepos={repos ?? []}
      />
    </div>
  );
}
