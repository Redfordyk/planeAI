/**
 * planeAI — Multi-Agent Orchestrator route (phase 12.1).
 *
 * Matches the drafts/page.tsx pattern exactly:
 *   - default export component function
 *   - Route.ComponentProps from RR7 typed routes
 *   - PageHead + content wrapper
 *
 * Workspace UUID is resolved from the slug via the mobx store. We
 * gate the heavy <OrchestratorPage /> behind a mounted guard so the
 * server-rendered shell and the first client paint emit the same
 * markup (avoids React errors #418 / #423).
 */

import { useEffect, useState } from "react";
import { observer } from "mobx-react";
import { Loader2 } from "lucide-react";
import { PageHead } from "@/components/core/page-title";
import { OrchestratorPage } from "@/components/ai";
import { useWorkspace } from "@/hooks/store/use-workspace";
import type { Route } from "./+types/page";

const AIOrchestratorPage = observer(function AIOrchestratorPage({ params }: Route.ComponentProps) {
  const { workspaceSlug } = params;
  const { getWorkspaceBySlug } = useWorkspace();

  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  const ws = mounted && workspaceSlug ? getWorkspaceBySlug(workspaceSlug) : null;
  const workspaceId = (ws as { id?: string } | null)?.id ?? "";

  return (
    <>
      <PageHead title="ИИ-оркестратор" />
      <div className="relative h-full w-full overflow-hidden overflow-y-auto bg-layer-1">
        {!mounted ? (
          <div className="flex h-full w-full items-center justify-center gap-2 text-body-sm text-tertiary">
            <Loader2 className="size-4 animate-spin" />
            Загружаю…
          </div>
        ) : workspaceId ? (
          <OrchestratorPage workspaceId={workspaceId} workspaceSlug={workspaceSlug} />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-body-sm text-tertiary">
            Не нашёл воркспейс.
          </div>
        )}
      </div>
    </>
  );
});

export default AIOrchestratorPage;
