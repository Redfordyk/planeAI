/**
 * Per-project AI on/off toggle for the General project settings page.
 *
 * Calls /api/ai/workspaces/<wid>/projects/<pid>/ai-settings/ via the
 * useProjectAISettings hook. When OFF, the project is excluded from
 * indexing, semantic search, and the agent/orchestrator.
 */

import { observer } from "mobx-react";
// plane imports
import { ToggleSwitch } from "@plane/ui";
// components
import { SettingsBoxedControlItem } from "@/components/settings/boxed-control-item";
// hooks
import { useProjectAISettings } from "@/hooks/ai/use-project-ai-settings";
import { useWorkspace } from "@/hooks/store/use-workspace";

type Props = {
  projectId: string;
  isAdmin: boolean;
};

export const ProjectAIToggle = observer(function ProjectAIToggle(props: Props) {
  const { projectId, isAdmin } = props;
  const { currentWorkspace } = useWorkspace();
  const workspaceId = currentWorkspace?.id;
  const { aiEnabled, loading, saving, error, setAIEnabled } = useProjectAISettings(
    workspaceId,
    projectId
  );

  const value = aiEnabled === null ? false : aiEnabled;
  const disabled = !isAdmin || loading || saving || aiEnabled === null;
  const description = error
    ? `AI: ${error}`
    : "When off, this project is excluded from search, the agent, and the orchestrator. Existing indexed content is purged.";

  return (
    <div className="mt-10">
      <SettingsBoxedControlItem
        title="AI"
        description={description}
        control={
          <ToggleSwitch
            value={value}
            onChange={() => setAIEnabled(!value)}
            disabled={disabled}
            size="sm"
          />
        }
      />
    </div>
  );
});
