export { AISearch, AISearchPanel } from "./ai-search";
export type { AISearchProps } from "./ai-search";

// TZ 5.6 — agent transparency UI primitives. Mount the composite
// page (AgentPage) at a workspace-scoped route, or use the lower-level
// pieces individually (feed in a side panel, badge on issue cards).
export { AgentActivityFeed } from "./agent-activity-feed";
export type { AgentActivityFeedProps } from "./agent-activity-feed";
export { AgentBadge, AgentBadgeAutoload } from "./agent-badge";
export type { AgentBadgeProps } from "./agent-badge";
export { AgentToggle } from "./agent-toggle";
export type { AgentToggleProps } from "./agent-toggle";
export { AgentPage } from "./agent-page";
export type { AgentPageProps } from "./agent-page";

// Phase 12.1 — Multi-Agent Orchestrator UI (Goals + Activity + Risks + Kill-switch).
export { OrchestratorPage } from "./orchestrator-page";
