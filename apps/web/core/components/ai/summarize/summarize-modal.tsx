/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useEffect } from "react";
// plane imports
import { useTranslation } from "@plane/i18n";
import { Button } from "@plane/propel/button";
import { ModalCore } from "@plane/ui";
// hooks
import { useIssueSummarize } from "@/hooks/ai/use-issue-summarize";

type Props = {
  isOpen: boolean;
  onClose: () => void;
  workspaceId: string | undefined;
  issueId: string | undefined;
};

export function SummarizeModal(props: Props) {
  const { isOpen, onClose, workspaceId, issueId } = props;
  const { t } = useTranslation();
  const { summary, status, cached, model, updatedAt, error, summarize, cancel } =
    useIssueSummarize(workspaceId, issueId);

  // Kick off the summarize automatically when the modal opens.
  useEffect(() => {
    if (!isOpen) return;
    if (!workspaceId || !issueId) return;
    void summarize({ force: false });
    return () => cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isOpen, workspaceId, issueId]);

  const handleClose = () => {
    cancel();
    onClose();
  };

  const handleCopy = async () => {
    if (!summary) return;
    try {
      await navigator.clipboard.writeText(summary);
    } catch {
      // clipboard write can fail in non-secure contexts; swallow silently.
    }
  };

  const handleRegenerate = () => {
    void summarize({ force: true });
  };

  return (
    <ModalCore isOpen={isOpen} handleClose={handleClose}>
      <div className="flex flex-col gap-3 p-5">
        <div className="flex items-baseline justify-between gap-3">
          <h3 className="text-lg font-semibold text-primary">{t("summarize.modal_title")}</h3>
          {cached && updatedAt && (
            <span className="text-11 text-tertiary" title={updatedAt}>
              {t("summarize.from_cache")}
            </span>
          )}
        </div>

        <div className="min-h-[120px] max-h-[60vh] overflow-y-auto rounded-md bg-layer-2 px-4 py-3 text-13 text-secondary whitespace-pre-wrap">
          {status === "streaming" && !summary ? (
            <span className="text-tertiary">{t("summarize.generating")}</span>
          ) : status === "error" ? (
            <span className="text-danger-primary">{error || t("summarize.generic_error")}</span>
          ) : (
            summary || <span className="text-tertiary">{t("summarize.empty")}</span>
          )}
        </div>

        {model && (
          <div className="text-11 text-tertiary">
            {t("summarize.model_label")}: <span className="font-medium">{model}</span>
          </div>
        )}

        <div className="flex items-center justify-end gap-2 pt-2">
          <Button variant="neutral-primary" onClick={handleCopy} disabled={!summary}>
            {t("summarize.copy")}
          </Button>
          <Button
            variant="neutral-primary"
            onClick={handleRegenerate}
            disabled={status === "streaming"}
          >
            {t("summarize.regenerate")}
          </Button>
          <Button variant="primary" onClick={handleClose}>
            {t("close")}
          </Button>
        </div>
      </div>
    </ModalCore>
  );
}
