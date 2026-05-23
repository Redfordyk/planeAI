/**
 * AgentBadge — small "🤖 ИИ" pill that decorates issue cards when
 * the autonomous agent has applied a durable action on the issue.
 *
 * Designed to be embedded in two places:
 *
 *   - **Issue card / list row** — single-issue lookup via the parent
 *     resolving a `touched` map upstream (preferred — one batched
 *     request per page) and passing `touched` as a prop.
 *
 *   - **Issue detail header** — a `<AgentBadgeAutoload>` variant that
 *     resolves itself by hitting `/issues/touched/?ids=` for a single
 *     id. Acceptable for one issue at a time; do NOT use this in a
 *     list (each card would issue its own request).
 *
 * The badge is purely an indicator — clicking it opens the agent
 * activity feed filtered to the issue, but that wiring lives in the
 * page that mounts the badge (we'd need router context here otherwise).
 */

import { useIssuesTouched } from "../../hooks/ai/use-agent-feed";

export type AgentBadgeProps = {
  touched: boolean;
  /** Optional click handler — typically opens the feed scoped to this issue. */
  onClick?: () => void;
  className?: string;
};

export function AgentBadge({ touched, onClick, className = "" }: AgentBadgeProps) {
  if (!touched) return null;
  const base =
    "inline-flex items-center gap-1 rounded-full bg-custom-background-80 px-2 py-0.5 text-[10px] font-medium text-custom-text-200";
  if (!onClick) {
    return (
      <span className={`${base} ${className}`} title="Действие ИИ">
        🤖 ИИ
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      className={`${base} hover:bg-custom-background-70 ${className}`}
      title="Действия ИИ на этой задаче — открыть ленту"
    >
      🤖 ИИ
    </button>
  );
}

/**
 * One-shot self-resolving variant. Renders nothing while loading
 * and nothing when the badge would be false; renders the badge
 * exactly when the backend says this issue was touched. Use
 * sparingly — see component-level doc above for why.
 */
export function AgentBadgeAutoload({
  workspaceId,
  issueId,
  onClick,
  className,
}: {
  workspaceId: string;
  issueId: string;
  onClick?: () => void;
  className?: string;
}) {
  const { touched } = useIssuesTouched(workspaceId, [issueId]);
  return (
    <AgentBadge
      touched={Boolean(touched[issueId])}
      onClick={onClick}
      className={className}
    />
  );
}
