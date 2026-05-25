/**
 * planeAI — Layout wrapper for /<workspaceSlug>/ai-orchestrator/.
 * Same structure as drafts/layout.tsx so we inherit Plane's header
 * row, content wrapper paddings, and side-nav behaviour.
 */

import { Outlet } from "react-router";
import { AppHeader } from "@/components/core/app-header";
import { ContentWrapper } from "@/components/core/content-wrapper";
import { AIOrchestratorHeader } from "./header";

export default function AIOrchestratorLayout() {
  return (
    <>
      <AppHeader header={<AIOrchestratorHeader />} />
      <ContentWrapper>
        <Outlet />
      </ContentWrapper>
    </>
  );
}
