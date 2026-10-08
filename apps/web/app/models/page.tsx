export const dynamic = "force-dynamic";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type HealthState = "ok" | "degraded" | "disabled";

type ModelRoute = {
  alias: string;
  provider: string;
  endpoint_ref: string;
  model_ref: string;
  priority: number;
  enabled: boolean;
  health_state: HealthState;
};

async function loadRoutes(): Promise<ModelRoute[]> {
  try {
    const response = await fetch(`${API_BASE}/models`, { cache: "no-store" });
    if (!response.ok) return [];
    const data: unknown = await response.json();
    return Array.isArray(data) ? (data as ModelRoute[]) : [];
  } catch {
    return [];
  }
}

const HEALTH_CLASSES: Record<HealthState, string> = {
  ok: "rounded bg-emerald-100 px-2 py-0.5 text-emerald-800",
  degraded: "rounded bg-amber-100 px-2 py-0.5 text-amber-800",
  disabled: "rounded bg-slate-200 px-2 py-0.5 text-slate-600",
};

export default async function ModelsPage() {
  const routes = await loadRoutes();
  const aliases = new Map<string, ModelRoute[]>();
  for (const route of routes) {
    const existing = aliases.get(route.alias) ?? [];
    existing.push(route);
    aliases.set(route.alias, existing);
  }
  return (
    <main className="mx-auto max-w-4xl p-8">
      <h1 className="mb-6 text-2xl font-bold">Model Registry</h1>
      {aliases.size === 0 ? (
        <p className="text-slate-500">No model aliases registered.</p>
      ) : (
        <ul className="space-y-4">
          {[...aliases.entries()].map(([alias, aliasRoutes]) => (
            <li key={alias} className="rounded-lg border p-4">
              <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-lg font-semibold">{alias}</h2>
                <p className="text-sm text-slate-600">
                  routes: {aliasRoutes.length} · providers:{" "}
                  {[...new Set(aliasRoutes.map((r) => r.provider))].join(", ")}
                </p>
              </div>
              <ol className="space-y-2">
                {aliasRoutes.map((route) => (
                  <li
                    key={`${route.provider}:${route.priority}`}
                    className="flex flex-wrap items-center gap-2 border-t pt-2 text-sm"
                  >
                    <span className="font-medium">#{route.priority}</span>
                    <span>{route.provider}</span>
                    <span className={HEALTH_CLASSES[route.health_state] ?? HEALTH_CLASSES.disabled}>
                      {route.health_state}
                    </span>
                    {!route.enabled && <span className="text-slate-500">disabled</span>}
                  </li>
                ))}
              </ol>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
