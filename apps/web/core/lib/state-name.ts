/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

/**
 * Translate a workspace State's display name for the UI.
 *
 * Plane seeds five (six with Triage) default `db.State` rows in
 * English on project creation — see apps/api/plane/db/models/state.py
 * `DEFAULT_STATES`. The seeding code has no locale awareness, so the
 * rows are always created with English names regardless of the user's
 * language preference.
 *
 * This helper compares a state name against the well-known English
 * defaults. If it matches, it returns the localised name from the
 * common.state_defaults dictionary. If it doesn't (i.e. the user has
 * renamed the state, or created a custom one), it returns the original
 * name untouched.
 *
 * Use this at *display* sites only — never at edit / rename inputs
 * where the canonical raw name must be visible.
 */

const DEFAULT_STATE_NAMES = new Set([
  "Backlog",
  "Todo",
  "In Progress",
  "Done",
  "Cancelled",
  "Triage",
]);

type TFunc = (key: string, params?: Record<string, unknown>) => string;

export function translateStateName(name: string | undefined | null, t: TFunc): string {
  if (!name) return "";
  if (!DEFAULT_STATE_NAMES.has(name)) return name;
  // Lookup goes through the default common namespace —
  // state_defaults is keyed by the English label verbatim.
  return t(`state_defaults.${name}`);
}
