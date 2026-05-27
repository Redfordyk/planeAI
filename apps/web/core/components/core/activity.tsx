/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect } from "react";
import { observer } from "mobx-react";
import { useParams } from "next/navigation";
// store hooks
// icons
import {
  TagIcon,
  CopyPlus,
  Calendar,
  Link2Icon,
  Users2Icon,
  ArchiveIcon,
  PaperclipIcon,
  TriangleIcon,
  LayoutGridIcon,
  SignalMediumIcon,
  MessageSquareIcon,
  UsersIcon,
} from "lucide-react";
import { useTranslation } from "@plane/i18n";
import { translateStateName } from "@/lib/state-name";
import {
  BlockedIcon,
  BlockerIcon,
  CycleIcon,
  EpicIcon,
  IntakeIcon,
  ModuleIcon,
  RelatedIcon,
  WorkItemsIcon,
} from "@plane/propel/icons";
import { Tooltip } from "@plane/propel/tooltip";
import type { IIssueActivity } from "@plane/types";
import { renderFormattedDate, generateWorkItemLink, capitalizeFirstLetter } from "@plane/utils";

type TFunc = (key: string, params?: Record<string, unknown>) => string;
// helpers
import { useLabel } from "@/hooks/store/use-label";
import { usePlatformOS } from "@/hooks/use-platform-os";
// types

export function IssueLink({ activity }: { activity: IIssueActivity }) {
  // router params
  const { workspaceSlug } = useParams();
  const { isMobile } = usePlatformOS();
  const { t } = useTranslation();

  const workItemLink = generateWorkItemLink({
    workspaceSlug: workspaceSlug?.toString() ?? activity.workspace_detail?.slug,
    projectId: activity?.project,
    issueId: activity?.issue,
    projectIdentifier: activity?.project_detail?.identifier,
    sequenceId: activity?.issue_detail?.sequence_id,
  });

  return (
    <Tooltip
      tooltipContent={
        activity?.issue_detail ? activity.issue_detail.name : t("activity_feed.issue_link_deleted")
      }
      isMobile={isMobile}
    >
      {activity?.issue_detail ? (
        <a
          aria-disabled={activity.issue === null}
          href={workItemLink}
          target={activity.issue === null ? "_self" : "_blank"}
          rel={activity.issue === null ? "" : "noopener noreferrer"}
          className="inline items-center gap-1 font-medium text-primary hover:underline"
        >
          <span className="whitespace-nowrap">{`${activity.project_detail.identifier}-${activity.issue_detail.sequence_id}`}</span>{" "}
          <span className="font-regular break-all">{activity.issue_detail?.name}</span>
        </a>
      ) : (
        <span className="inline-flex items-center gap-1 font-medium whitespace-nowrap text-primary">
          {" "}{t("activity_feed.a_work_item")}{" "}
        </span>
      )}
    </Tooltip>
  );
}

function UserLink({ activity }: { activity: IIssueActivity }) {
  // router params
  const { workspaceSlug } = useParams();

  return (
    <a
      href={`/${workspaceSlug ?? activity.workspace_detail?.slug}/profile/${
        activity.new_identifier ?? activity.old_identifier
      }`}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center font-medium text-primary hover:underline"
    >
      {activity.new_value && activity.new_value !== "" ? activity.new_value : activity.old_value}
    </a>
  );
}

const LabelPill = observer(function LabelPill({ labelId, workspaceSlug }: { labelId: string; workspaceSlug: string }) {
  // store hooks
  const { workspaceLabels, fetchWorkspaceLabels } = useLabel();

  useEffect(() => {
    if (!workspaceLabels) fetchWorkspaceLabels(workspaceSlug);
  }, [fetchWorkspaceLabels, workspaceLabels, workspaceSlug]);

  return (
    <span
      className="h-1.5 w-1.5 flex-shrink-0 rounded-full"
      style={{
        backgroundColor: workspaceLabels?.find((l) => l.id === labelId)?.color ?? "#000000",
      }}
      aria-hidden="true"
    />
  );
});

const getInboxUserActivityMessage = (activity: IIssueActivity, showIssue: boolean, t: TFunc) => {
  switch (activity.verb) {
    case "-1":
      return t(showIssue ? "activity_feed.inbox.declined_with_issue" : "activity_feed.inbox.declined_no_issue");
    case "0":
      return t(showIssue ? "activity_feed.inbox.snoozed_with_issue" : "activity_feed.inbox.snoozed_no_issue");
    case "1":
      return t(showIssue ? "activity_feed.inbox.accepted_with_issue" : "activity_feed.inbox.accepted_no_issue");
    case "2":
      // "markedDuplicate" shares the declined-with-issue text but
      // appends a clarifying suffix via the inbox branch below.
      return t(showIssue ? "activity_feed.inbox.declined_with_issue" : "activity_feed.inbox.declined_no_issue");
    default:
      return t("activity_feed.updated_intake_status");
  }
};

const activityDetails: {
  [key: string]: {
    message: (activity: IIssueActivity, showIssue: boolean, workspaceSlug: string, t: TFunc) => React.ReactNode;
    icon: React.ReactNode;
  };
} = {
  assignees: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.old_value === "")
        return (
          <>
            {t("activity_feed.assignees.added")} <UserLink activity={activity} />
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.to")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.assignees.removed")} <UserLink activity={activity} />
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <Users2Icon size={12} className="text-secondary" aria-hidden="true" />,
  },
  archived_at: {
    message: (activity, _showIssue, _ws, t) => {
      if (activity.new_value === "restore")
        return (
          <>
            {t("activity_feed.archive.restored")} <IssueLink activity={activity} />
          </>
        );
      else
        return (
          <>
            {t("activity_feed.archive.archived")} <IssueLink activity={activity} />
          </>
        );
    },
    icon: <ArchiveIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  attachment: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.verb === "created")
        return (
          <>
            {t("activity_feed.attachment.uploaded")}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.to")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.attachment.removed")}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <PaperclipIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  description: {
    message: (activity, showIssue, _ws, t) => (
      <>
        {t("activity_feed.description.updated")}
        {showIssue && (
          <>
            {" "}
            {t("activity_feed.preposition.of")} <IssueLink activity={activity} />
          </>
        )}
      </>
    ),
    icon: <MessageSquareIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  estimate_point: {
    message: (activity, showIssue, _ws, t) => {
      if (!activity.new_value)
        return (
          <>
            {t("activity_feed.estimate.removed")}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.estimate.set_to")} {activity.new_value}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.for")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <TriangleIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  issue: {
    message: (activity, _showIssue, _ws, t) => {
      if (activity.verb === "created")
        return (
          <>
            {t("activity_feed.issue.created")} <IssueLink activity={activity} />
          </>
        );
      else if (activity.verb === "converted")
        return (
          <>
            {t("activity_feed.issue.converted_prefix")} <IssueLink activity={activity} />{" "}
            {t("activity_feed.issue.converted_to_epic")}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.issue.deleted")} <IssueLink activity={activity} />
          </>
        );
    },
    icon: <WorkItemsIcon width={12} height={12} className="text-secondary" aria-hidden="true" />,
  },
  epic: {
    message: (activity, _showIssue, _ws, t) => {
      if (activity.verb === "created")
        return (
          <>
            {t("activity_feed.issue.created")} <IssueLink activity={activity} />
          </>
        );
      else if (activity.verb === "converted")
        return (
          <>
            {t("activity_feed.issue.converted_prefix")} <IssueLink activity={activity} />{" "}
            {t("activity_feed.issue.converted_to_issue")}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.issue.deleted")} <IssueLink activity={activity} />
          </>
        );
    },
    icon: <EpicIcon width={12} height={12} className="text-secondary" aria-hidden="true" />,
  },
  labels: {
    message: (activity, showIssue, workspaceSlug, t) => {
      if (activity.old_value === "")
        return (
          <span className="overflow-hidden">
            {t("activity_feed.label.added")}{" "}
            <span className="inline-flex items-center gap-2 rounded-full border border-strong px-2 py-0.5 text-11">
              <LabelPill labelId={activity.new_identifier ?? ""} workspaceSlug={workspaceSlug} />
              <span className="line-clamp-1 flex-shrink font-medium break-all text-primary">{activity.new_value}</span>
            </span>
            {showIssue && (
              <span className="">
                {" "}
                {t("activity_feed.preposition.to")} <IssueLink activity={activity} />
              </span>
            )}
          </span>
        );
      else
        return (
          <>
            {t("activity_feed.label.removed")}{" "}
            <span className="inline-flex items-center gap-2 rounded-full border border-strong px-2 py-0.5 text-11">
              <LabelPill labelId={activity.old_identifier ?? ""} workspaceSlug={workspaceSlug} />
              <span className="line-clamp-1 flex-shrink font-medium break-all text-primary">{activity.old_value}</span>
            </span>
            {showIssue && (
              <span>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </span>
            )}
          </>
        );
    },
    icon: <TagIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  link: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.verb === "created")
        return (
          <>
            {t("activity_feed.link.added")}{" "}
            <a
              href={`${activity.new_value}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
            >
              {t("activity_feed.link.link_word")}
            </a>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.to")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else if (activity.verb === "updated")
        return (
          <>
            {t("activity_feed.link.updated")}{" "}
            <a
              href={`${activity.old_value}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
            >
              {t("activity_feed.link.link_word")}
            </a>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.link.removed")}{" "}
            <a
              href={`${activity.old_value}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex items-center gap-1 font-medium text-primary hover:underline"
            >
              {t("activity_feed.link.link_word")}
            </a>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <Link2Icon size={12} className="text-secondary" aria-hidden="true" />,
  },
  cycles: {
    message: (activity, showIssue, workspaceSlug, t) => {
      if (activity.verb === "created")
        return (
          <>
            <span className="flex-shrink-0">
              {t("activity_feed.cycle.added_prefix")}{" "}
              {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
              <span className="whitespace-nowrap">{t("activity_feed.cycle.to_the_cycle")}</span>{" "}
            </span>
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/cycles/${activity.new_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.new_value}</span>
            </a>
          </>
        );
      else if (activity.verb === "updated")
        return (
          <>
            <span className="flex-shrink-0 whitespace-nowrap">{t("activity_feed.cycle.set_to")} </span>
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/cycles/${activity.new_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.new_value}</span>
            </a>
          </>
        );
      else
        return (
          <>
            {t("activity_feed.cycle.removed_prefix")} <IssueLink activity={activity} />{" "}
            {t("activity_feed.cycle.from_the_cycle")}{" "}
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/cycles/${activity.old_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.old_value}</span>
            </a>
          </>
        );
    },
    icon: <CycleIcon height={12} width={12} className="text-secondary" aria-hidden="true" />,
  },
  modules: {
    message: (activity, showIssue, workspaceSlug, t) => {
      if (activity.verb === "created")
        return (
          <>
            {t("activity_feed.module.added_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.module.to_the_module")}{" "}
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/modules/${activity.new_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.new_value}</span>
            </a>
          </>
        );
      else if (activity.verb === "updated")
        return (
          <>
            {t("activity_feed.module.set_to")}{" "}
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/modules/${activity.new_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.new_value}</span>
            </a>
          </>
        );
      else
        return (
          <>
            {t("activity_feed.module.removed_prefix")} <IssueLink activity={activity} />{" "}
            {t("activity_feed.module.from_the_module")}{" "}
            <a
              href={`/${workspaceSlug}/projects/${activity.project}/modules/${activity.old_identifier}`}
              target="_blank"
              rel="noopener noreferrer"
              className="inline items-center gap-1 font-medium text-primary hover:underline"
            >
              <span className="break-all">{activity.old_value}</span>
            </a>
          </>
        );
    },
    icon: <ModuleIcon className="h-3 w-3 !text-secondary" aria-hidden="true" />,
  },
  name: {
    message: (activity, showIssue, _ws, t) => (
      <>
        {t("activity_feed.name.set_to")} <span className="break-all">{activity.new_value}</span>
        {showIssue && (
          <>
            {" "}
            {t("activity_feed.preposition.of")} <IssueLink activity={activity} />
          </>
        )}
      </>
    ),
    icon: <MessageSquareIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  parent: {
    message: (activity, showIssue, _ws, t) => {
      if (!activity.new_value)
        return (
          <>
            {t("activity_feed.parent.removed")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.old_value}</span>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.parent.set_to")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.new_value}</span>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.for")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <UsersIcon className="h-3 w-3 !text-secondary" aria-hidden="true" />,
  },
  priority: {
    message: (activity, showIssue, _ws, t) => (
      <>
        {t("activity_feed.priority.set_to")}{" "}
        <span className="font-medium text-primary">
          {activity.new_value ? capitalizeFirstLetter(activity.new_value) : t("activity_feed.priority.none")}
        </span>
        {showIssue && (
          <>
            {" "}
            {t("activity_feed.preposition.for")} <IssueLink activity={activity} />
          </>
        )}
      </>
    ),
    icon: <SignalMediumIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  relates_to: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.old_value === "")
        return (
          <>
            {t("activity_feed.relates_to.added_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.relates_to.added_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.new_value}</span>.
          </>
        );
      else
        return (
          <>
            {t("activity_feed.relates_to.removed")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.old_value}</span>.
          </>
        );
    },
    icon: <RelatedIcon height="12" width="12" className="text-secondary" />,
  },
  blocking: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.old_value === "")
        return (
          <>
            {t("activity_feed.blocking.added_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.blocking.added_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.new_value}</span>.
          </>
        );
      else
        return (
          <>
            {t("activity_feed.blocking.removed")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.old_value}</span>.
          </>
        );
    },
    icon: <BlockerIcon height="12" width="12" className="text-secondary" />,
  },
  blocked_by: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.old_value === "")
        return (
          <>
            {t("activity_feed.blocked_by.added_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.blocked_by.added_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.new_value}</span>.
          </>
        );
      else
        return (
          <>
            {t("activity_feed.blocked_by.removed_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.blocked_by.removed_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.old_value}</span>.
          </>
        );
    },
    icon: <BlockedIcon height="12" width="12" className="text-secondary" />,
  },
  duplicate: {
    message: (activity, showIssue, _ws, t) => {
      if (activity.old_value === "")
        return (
          <>
            {t("activity_feed.duplicate.added_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.duplicate.added_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.new_value}</span>.
          </>
        );
      else
        return (
          <>
            {t("activity_feed.duplicate.removed_prefix")}{" "}
            {showIssue ? <IssueLink activity={activity} /> : t("activity_feed.this_work_item")}{" "}
            {t("activity_feed.duplicate.removed_suffix")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">{activity.old_value}</span>.
          </>
        );
    },
    icon: <CopyPlus size={12} className="text-secondary" />,
  },
  state: {
    message: (activity, showIssue, _ws, t) => (
      <>
        {t("activity_feed.state.set_to")}{" "}
        <span className="font-medium break-all text-primary">{translateStateName(activity.new_value, t)}</span>
        {showIssue && (
          <>
            {" "}
            {t("activity_feed.preposition.for")} <IssueLink activity={activity} />
          </>
        )}
      </>
    ),
    icon: <LayoutGridIcon size={12} className="text-secondary" aria-hidden="true" />,
  },
  start_date: {
    message: (activity, showIssue, _ws, t) => {
      if (!activity.new_value)
        return (
          <>
            {t("activity_feed.start_date.removed")}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.start_date.set_to")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">
              {renderFormattedDate(activity.new_value)}
            </span>
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.for")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <Calendar size={12} className="text-secondary" aria-hidden="true" />,
  },
  target_date: {
    message: (activity, showIssue, _ws, t) => {
      if (!activity.new_value)
        return (
          <>
            {t("activity_feed.target_date.removed")}
            {showIssue && (
              <>
                {" "}
                {t("activity_feed.preposition.from")} <IssueLink activity={activity} />
              </>
            )}
          </>
        );
      else
        return (
          <>
            {t("activity_feed.target_date.set_to")}{" "}
            <span className="font-medium whitespace-nowrap text-primary">
              {renderFormattedDate(activity.new_value)}
            </span>
            {showIssue && (
              <>
                <IssueLink activity={activity} />
              </>
            )}
          </>
        );
    },
    icon: <Calendar size={12} className="text-secondary" aria-hidden="true" />,
  },
  inbox: {
    message: (activity, showIssue, _ws, t) => (
      <>
        {getInboxUserActivityMessage(activity, showIssue, t)}
        {showIssue && (
          <>
            {" "}
            <IssueLink activity={activity} />
          </>
        )}
        {activity.verb === "2" && t("activity_feed.from_intake_by_duplicate")}
      </>
    ),
    icon: <IntakeIcon className="size-3 text-secondary" aria-hidden="true" />,
  },
};

export function ActivityIcon({ activity }: { activity: IIssueActivity }) {
  return <>{activityDetails[activity.field as keyof typeof activityDetails]?.icon}</>;
}

type ActivityMessageProps = {
  activity: IIssueActivity;
  showIssue?: boolean;
};

export function ActivityMessage({ activity, showIssue = false }: ActivityMessageProps) {
  // router params
  const { workspaceSlug } = useParams();
  const { t } = useTranslation();
  const activityField = activity.field ?? "issue";

  return (
    <>
      {activityDetails[activityField as keyof typeof activityDetails]?.message(
        activity,
        showIssue,
        workspaceSlug ? workspaceSlug.toString() : (activity.workspace_detail?.slug ?? ""),
        t
      )}
    </>
  );
}
