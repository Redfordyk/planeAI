/**
 * planeAI — Multi-Agent Orchestrator page (phase 12.1).
 *
 * Mounted at /{workspaceSlug}/ai-orchestrator/. Resolves workspace
 * UUID from the slug via useWorkspace() store, then renders the
 * full <OrchestratorPage /> (goals, agents activity, risks,
 * kill-switch).
 *
 * Client-only because all the data comes from /api/ai/* with the
 * session cookie — there's nothing to SSR.
 */

"use client";

import { observer } from "mobx-react";
import { PageHead } from "@/components/core/page-title";
import { OrchestratorPage } from "@/components/ai";
import { useWorkspace } from "@/hooks/store/use-workspace";
import type { Route } from "./+types/page";

const Page = observer(function Page({ params }: Route.ComponentProps) {
  const { workspaceSlug } = params;
  const { getWorkspaceBySlug } = useWorkspace();
  const ws = workspaceSlug ? getWorkspaceBySlug(workspaceSlug.toString()) : null;
  const workspaceId = (ws as { id?: string } | null)?.id ?? "";

  return (
    <>
      <PageHead title="ИИ-оркестратор" />
      <div className="relative h-full w-full overflow-hidden overflow-y-auto">
        {workspaceId ? (
          <OrchestratorPage workspaceId={workspaceId} workspaceSlug={workspaceSlug.toString()} />
        ) : (
          <div className="flex h-full w-full items-center justify-center text-sm text-custom-text-300">
            Загружаю...
          </div>
        )}
      </div>
    </>
  );
});

export default Page;
