/**
 * planeAI — Multi-Agent Orchestrator route (phase 12.1).
 *
 * Mounted at /{workspaceSlug}/ai-orchestrator/. Resolves workspace
 * UUID from the slug via useWorkspace() store. Mount is gated on a
 * client-side `mounted` flag so SSR markup matches the first client
 * paint — without it React reports hydration errors #418 / #423
 * because getWorkspaceBySlug() returns null on the server and a
 * populated object on the client.
 */

"use client";

import { useEffect, useState } from "react";
import { observer } from "mobx-react";
import { useParams } from "react-router";
import { Loader2 } from "lucide-react";
import { PageHead } from "@/components/core/page-title";
import { OrchestratorPage } from "@/components/ai";
import { useWorkspace } from "@/hooks/store/use-workspace";

const Page = observer(function Page() {
  const params = useParams<{ workspaceSlug: string }>();
  const workspaceSlug = params.workspaceSlug ?? "";
  const { getWorkspaceBySlug } = useWorkspace();

  // Hydration guard — SSR has no workspace store, client does.
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

export default Page;
