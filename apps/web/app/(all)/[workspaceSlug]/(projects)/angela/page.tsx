/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { observer } from "mobx-react";
// plane imports
import { useTranslation } from "@plane/i18n";
// components
import { ContentWrapper } from "@/components/core/content-wrapper";
import { PageHead } from "@/components/core/page-title";
import { AngelaConsole } from "@/components/angela/angela-console";
// hooks
import { useWorkspace } from "@/hooks/store/use-workspace";

function AngelaPage() {
  const { currentWorkspace } = useWorkspace();
  const { t } = useTranslation();
  const pageTitle = currentWorkspace?.name ? `${currentWorkspace?.name} - ${t("angela.title")}` : "Angela";

  return (
    <ContentWrapper>
      <PageHead title={pageTitle} />
      <AngelaConsole />
    </ContentWrapper>
  );
}

export default observer(AngelaPage);
