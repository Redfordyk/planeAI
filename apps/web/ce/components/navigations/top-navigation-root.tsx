/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 *
 * planeAI:
 *   - removed StarUsOnGitHubLink import + usage from the top nav.
 *   - added an "ИИ-помощник" button that toggles AISearchPanel —
 *     workspace UUID resolved via useWorkspace() from the slug in
 *     the URL.
 */

import React, { useEffect, useState } from "react";
import { observer } from "mobx-react";
import { useParams, usePathname } from "next/navigation";
import { cn } from "@plane/utils";
import { TopNavPowerK } from "@/components/navigation";
import { HelpMenuRoot } from "@/components/workspace/sidebar/help-section/root";
import { UserMenuRoot } from "@/components/workspace/sidebar/user-menu-root";
import { WorkspaceMenuRoot } from "@/components/workspace/sidebar/workspace-menu-root";
import { useAppRailPreferences } from "@/hooks/use-navigation-preferences";
import { Tooltip } from "@plane/propel/tooltip";
import { AppSidebarItem } from "@/components/sidebar/sidebar-item";
import { InboxIcon } from "@plane/propel/icons";
import useSWR from "swr";
import { useWorkspaceNotifications } from "@/hooks/store/notifications";
import { useWorkspace } from "@/hooks/store/use-workspace";
// planeAI
import { AISearchPanel } from "@/components/ai";

export const TopNavigationRoot = observer(function TopNavigationRoot() {
  // router
  const { workspaceSlug } = useParams();
  const pathname = usePathname();

  // store hooks
  const { unreadNotificationsCount, getUnreadNotificationsCount } = useWorkspaceNotifications();
  const { preferences } = useAppRailPreferences();
  const { getWorkspaceBySlug } = useWorkspace();

  // planeAI: visible AI panel state. Mounted=false on first render
  // (SSR + initial client paint) so we don't emit different markup
  // than the server did — React's hydration check (errors #418/#423)
  // is otherwise flaky because getWorkspaceBySlug() returns null on
  // the server and a populated object on the client.
  const [aiOpen, setAiOpen] = useState(false);
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  const slugStr = workspaceSlug?.toString() ?? "";
  const currentWorkspace = slugStr ? getWorkspaceBySlug(slugStr) : null;
  const currentWorkspaceId = (currentWorkspace as { id?: string } | null)?.id ?? "";
  const showAIButton = mounted && Boolean(currentWorkspaceId);

  const showLabel = preferences.displayMode === "icon_with_label";

  // Fetch notification count
  useSWR(
    workspaceSlug ? "WORKSPACE_UNREAD_NOTIFICATION_COUNT" : null,
    workspaceSlug ? () => getUnreadNotificationsCount(workspaceSlug.toString()) : null
  );

  // Calculate notification count
  const isMentionsEnabled = unreadNotificationsCount.mention_unread_notifications_count > 0;
  const totalNotifications = isMentionsEnabled
    ? unreadNotificationsCount.mention_unread_notifications_count
    : unreadNotificationsCount.total_unread_notifications_count;

  return (
    <div
      className={cn("z-[27] flex min-h-10 w-full items-center bg-canvas px-3.5 transition-all duration-300", {
        "px-2": !showLabel,
      })}
    >
      {/* Workspace Menu */}
      <div className="flex-1 shrink-0">
        <WorkspaceMenuRoot variant="top-navigation" />
      </div>
      {/* Power K Search */}
      <div className="shrink-0">
        <TopNavPowerK />
      </div>
      {/* Additional Actions */}
      <div className="flex flex-1 shrink-0 items-center justify-end gap-1">
        {/* planeAI: prominent AI assistant entry (client-only) */}
        {showAIButton && (
          <Tooltip tooltipContent="ИИ-помощник" position="bottom">
            <button
              type="button"
              onClick={() => setAiOpen(true)}
              className="flex h-8 items-center gap-1.5 rounded-md bg-custom-primary-100/10 px-2.5 text-xs font-medium text-custom-primary-100 hover:bg-custom-primary-100/20 transition-colors"
              aria-label="Открыть ИИ-помощника"
            >
              <span aria-hidden="true">✨</span>
              <span>ИИ</span>
            </button>
          </Tooltip>
        )}
        <Tooltip tooltipContent="Inbox" position="bottom">
          <AppSidebarItem
            variant="link"
            item={{
              href: `/${workspaceSlug?.toString()}/notifications/`,
              icon: (
                <div className="relative">
                  <InboxIcon className="size-5" />
                  {totalNotifications > 0 && (
                    <span className="absolute top-0 right-0 size-2 rounded-full bg-danger-primary" />
                  )}
                </div>
              ),
              isActive: pathname?.includes("/notifications/"),
            }}
          />
        </Tooltip>
        <HelpMenuRoot />
        {/* planeAI: removed <StarUsOnGitHubLink /> */}
        <div className="flex size-8 items-center justify-center rounded-md hover:bg-layer-1-hover">
          <UserMenuRoot />
        </div>
      </div>

      {/* planeAI: AI assistant slide-over (client-only) */}
      {showAIButton && (
        <AISearchPanel
          open={aiOpen}
          onClose={() => setAiOpen(false)}
          workspaceId={currentWorkspaceId}
          workspaceSlug={slugStr}
        />
      )}
    </div>
  );
});
