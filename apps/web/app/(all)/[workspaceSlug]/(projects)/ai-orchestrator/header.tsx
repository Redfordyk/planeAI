/**
 * planeAI — Header for the orchestrator route.
 * Mirrors the structure of drafts/header.tsx so it sits at the same
 * vertical position and uses the same Breadcrumbs primitive.
 */

import { Sparkles } from "lucide-react";
import { Breadcrumbs, Header } from "@plane/ui";
import { BreadcrumbLink } from "@/components/common/breadcrumb-link";

export function AIOrchestratorHeader() {
  return (
    <Header>
      <Header.LeftItem>
        <Breadcrumbs>
          <Breadcrumbs.Item
            component={
              <BreadcrumbLink
                label="ИИ-оркестратор"
                icon={<Sparkles className="size-4 text-accent-primary" />}
              />
            }
          />
        </Breadcrumbs>
      </Header.LeftItem>
    </Header>
  );
}
