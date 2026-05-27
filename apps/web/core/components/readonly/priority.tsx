/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// plane imports
import { ISSUE_PRIORITIES } from "@plane/constants";
import { useTranslation } from "@plane/i18n";
import { PriorityIcon } from "@plane/propel/icons";
import type { TIssuePriorities } from "@plane/types";
import { cn } from "@plane/utils";

export type TReadonlyPriorityProps = {
  className?: string;
  hideIcon?: boolean;
  value: TIssuePriorities | undefined | null;
  placeholder?: string;
};

export const ReadonlyPriority = observer(function ReadonlyPriority(props: TReadonlyPriorityProps) {
  const { className, hideIcon = false, value, placeholder } = props;

  const { t } = useTranslation();
  // ISSUE_PRIORITIES still imported so we narrow `value` to a valid
  // key before looking up the translation (and silently fall back to
  // common.none for any unexpected/empty value).
  const validKey = value && ISSUE_PRIORITIES.find((p) => p.key === value)?.key;
  const label =
    !validKey || validKey === "none"
      ? (placeholder ?? t("common.none"))
      : t(`issue.priority.${validKey}`);

  return (
    <div className={cn("flex items-center gap-1 text-body-xs-regular", className)}>
      {!hideIcon && <PriorityIcon priority={value ?? "none"} size={12} className="flex-shrink-0" withContainer />}
      <span className="flex-grow truncate">{label}</span>
    </div>
  );
});
