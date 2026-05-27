/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import type { TIssueActivity } from "@plane/types";

type TFunc = (key: string, params?: Record<string, unknown>) => string;

export const getRelationActivityContent = (
  activity: TIssueActivity | undefined,
  t?: TFunc
): string | undefined => {
  if (!activity) return;
  // Backwards-compat: if no translator is passed, fall back to the
  // historical English strings.
  const tr: TFunc = t ?? ((k: string) => k);
  const sp = " ";

  switch (activity.field) {
    case "blocking":
      return activity.old_value === ""
        ? t
          ? `${tr("activity_feed.blocking.added_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.blocking.added_suffix")}${sp}`
          : `marked this work item is blocking work item `
        : t
          ? `${tr("activity_feed.blocking.removed")}${sp}`
          : `removed the blocking work item `;
    case "blocked_by":
      return activity.old_value === ""
        ? t
          ? `${tr("activity_feed.blocked_by.added_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.blocked_by.added_suffix")}${sp}`
          : `marked this work item is being blocked by `
        : t
          ? `${tr("activity_feed.blocked_by.removed_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.blocked_by.removed_suffix")}${sp}`
          : `removed this work item being blocked by work item `;
    case "duplicate":
      return activity.old_value === ""
        ? t
          ? `${tr("activity_feed.duplicate.added_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.duplicate.added_suffix")}${sp}`
          : `marked this work item as duplicate of `
        : t
          ? `${tr("activity_feed.duplicate.removed_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.duplicate.removed_suffix")}${sp}`
          : `removed this work item as a duplicate of `;
    case "relates_to":
      return activity.old_value === ""
        ? t
          ? `${tr("activity_feed.relates_to.added_prefix")} ${tr("activity_feed.this_work_item")} ${tr("activity_feed.relates_to.added_suffix")}${sp}`
          : `marked that this work item relates to `
        : t
          ? `${tr("activity_feed.relates_to.removed")}${sp}`
          : `removed the relation from `;
  }

  return;
};
