/**
 * UsagePage — drop-in screen for TZ 6.3.
 *
 * Same pattern as ``AgentPage`` (TZ 5.6): wraps the dashboard with a
 * page header so the parent route file doesn't have to know about
 * spacing / copy / admin-banner. Mounted at a workspace-scoped admin
 * route (e.g. ``/<workspaceSlug>/settings/ai-usage``) — Plane CE's
 * route registration lives in ``apps/web/app/routes/extended.ts``,
 * which is environment-specific so we do NOT edit it here.
 *
 * If the parent already gates the route by admin role, the dashboard
 * still works for non-admins: the hook surfaces 403 as ``forbidden``
 * and the component renders an admin-only banner.
 */

import { UsageDashboard } from "./usage-dashboard";

export type UsagePageProps = {
  workspaceId: string;
  className?: string;
  /** ``{user_id: email}`` from the parent's user store. Optional. */
  userEmails?: Record<string, string>;
};

export function UsagePage({ workspaceId, className = "", userEmails }: UsagePageProps) {
  return (
    <div className={`flex flex-col gap-6 p-4 ${className}`}>
      <header className="flex flex-col gap-1">
        <h1 className="text-lg font-medium text-custom-text-100">
          ИИ-расход: токены и стоимость
        </h1>
        <p className="text-sm text-custom-text-300">
          Сколько стоит ИИ в этом воркспейсе, по фичам и
          пользователям. Цифры считаются по {" "}
          <code className="rounded bg-custom-background-80 px-1 py-0.5 text-xs">
            AIUsageLog
          </code>{" "}
          и совпадают с теми, что использует бюджет-гард.
        </p>
      </header>

      <UsageDashboard workspaceId={workspaceId} userEmails={userEmails} />
    </div>
  );
}
