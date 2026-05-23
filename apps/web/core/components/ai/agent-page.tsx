/**
 * AgentPage — drop-in transparency page for TZ 5.6.
 *
 * Composes the three primitives — feed, toggle, badge — into the
 * single screen the team uses to see what the agent has been doing.
 * Designed to be mounted at a workspace-scoped route (e.g.
 * `/<workspaceSlug>/settings/ai-agent`) from Plane's existing
 * `apps/web/app/routes/extended.ts` config; we don't add the route
 * entry here because mount points are environment-specific (Plane CE
 * vs the Cloud build).
 *
 * Props are intentionally minimal — `workspaceId`, `workspaceSlug`,
 * and `isAdmin` come from the parent's existing user / workspace
 * stores. If the caller has a project list to pass for filtering,
 * forward it via `projects`; otherwise the feed still works, just
 * without the project-scoped dropdown.
 */

import { AgentActivityFeed } from "./agent-activity-feed";
import { AgentToggle } from "./agent-toggle";

export type AgentPageProps = {
  workspaceId: string;
  workspaceSlug: string;
  isAdmin: boolean;
  projects?: { id: string; name: string }[];
  className?: string;
};

export function AgentPage({
  workspaceId,
  workspaceSlug,
  isAdmin,
  projects,
  className = "",
}: AgentPageProps) {
  return (
    <div className={`flex flex-col gap-6 p-4 ${className}`}>
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-medium text-custom-text-100">
          ИИ-агент: лента действий
        </h1>
        <p className="text-sm text-custom-text-300">
          Здесь видно, что агент сделал автономно: триаж, дубли,
          черновики описаний. Обратимые действия можно отменить
          одной кнопкой.
        </p>
      </header>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium text-custom-text-200">
          Состояние агента
        </h2>
        <AgentToggle workspaceId={workspaceId} isAdmin={isAdmin} />
      </section>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium text-custom-text-200">
          Лента действий
        </h2>
        <AgentActivityFeed
          workspaceId={workspaceId}
          workspaceSlug={workspaceSlug}
          projects={projects}
        />
      </section>
    </div>
  );
}
