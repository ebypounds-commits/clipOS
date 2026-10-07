export const dynamic = "force-dynamic";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

type AgentVersion = {
  version: string;
  activation_state: "DRAFT" | "EVALUATING" | "CANARY" | "ACTIVE" | "SUSPENDED" | "RETIRED";
  model_alias: string;
  fallback_aliases: string[];
};

type AgentRecord = {
  agent_id: string;
  active_version: string | null;
  versions: AgentVersion[];
};

const LIFECYCLE_CHAIN = [
  "DRAFT",
  "EVALUATING",
  "CANARY",
  "ACTIVE",
  "SUSPENDED",
  "RETIRED",
] as const;

type LifecycleState = (typeof LIFECYCLE_CHAIN)[number];

async function loadAgents(): Promise<AgentRecord[]> {
  try {
    const response = await fetch(`${API_BASE}/agents`, { cache: "no-store" });
    if (!response.ok) return [];
    const data: unknown = await response.json();
    return Array.isArray(data) ? (data as AgentRecord[]) : [];
  } catch {
    return [];
  }
}

function LifecycleChain({ current }: { current: LifecycleState }) {
  return (
    <ol className="flex flex-wrap items-center gap-1 text-xs">
      {LIFECYCLE_CHAIN.map((state, index) => (
        <li key={state} className="flex items-center gap-1">
          {index > 0 && <span aria-hidden>→</span>}
          <span
            className={
              state === current
                ? "rounded bg-slate-900 px-2 py-0.5 font-semibold text-white"
                : "rounded bg-slate-100 px-2 py-0.5 text-slate-500"
            }
          >
            {state}
          </span>
        </li>
      ))}
    </ol>
  );
}

export default async function AgentsPage() {
  const agents = await loadAgents();
  return (
    <main className="mx-auto max-w-4xl p-8">
      <h1 className="mb-6 text-2xl font-bold">Agent Registry</h1>
      {agents.length === 0 ? (
        <p className="text-slate-500">No agents registered.</p>
      ) : (
        <ul className="space-y-4">
          {agents.map((agent) => (
            <li key={agent.agent_id} className="rounded-lg border p-4">
              <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
                <h2 className="text-lg font-semibold">{agent.agent_id}</h2>
                <p className="text-sm text-slate-600">
                  active: {agent.active_version ?? "none"} · versions: {agent.versions.length}
                </p>
              </div>
              <ul className="space-y-3">
                {agent.versions.map((version) => (
                  <li key={version.version} className="border-t pt-3">
                    <p className="mb-1 text-sm">
                      <span className="font-medium">{version.version}</span>
                      <span className="text-slate-500"> · alias: {version.model_alias}</span>
                      {version.fallback_aliases.length > 0 && (
                        <span className="text-slate-500">
                          {" "}
                          · fallback: {version.fallback_aliases.join(", ")}
                        </span>
                      )}
                    </p>
                    <LifecycleChain current={version.activation_state} />
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
