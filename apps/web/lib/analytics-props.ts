// Pure helpers behind lib/analytics.ts (no imports, so lib/analytics-props.test.ts
// can run them under node).
//
//   shouldBridgeToNr    which trackEvent() events are sent to New Relic as
//                       PageActions (lib/newrelic.ts filters their PII); every
//                       other trackEvent() name is dropped in the browser.

type EventProps = Record<string, unknown>;

// Product events worth having next to the APM traces. Deliberately not the
// per-keystroke / per-field ones: the agent already records clicks, page
// views and AJAX on its own.
// recruiter_*: the recruiter-activity actions (lib/activity.ts), one per
// action, batched to the API separately.
export const NR_BRIDGE_PREFIXES = ["job_wizard_step5_", "launch_", "pair_launch_", "recruiter_"] as const;
export const NR_BRIDGE_EVENTS: ReadonlySet<string> = new Set(["job_wizard_step_completed"]);
// Prefix matches that fire per keystroke: the on-blur
// job_wizard_step5_boolean_edit_saved already records the final edit.
export const NR_BRIDGE_EXCLUDE: ReadonlySet<string> = new Set(["job_wizard_step5_boolean_edited"]);
// Bulky before/after snapshots of the wizard form (recruiter-entered text).
export const NR_BRIDGE_DROP_KEYS: ReadonlySet<string> = new Set(["changes"]);

export function shouldBridgeToNr(eventType: string): boolean {
  if (!eventType || NR_BRIDGE_EXCLUDE.has(eventType)) return false;
  if (NR_BRIDGE_EVENTS.has(eventType)) return true;
  return NR_BRIDGE_PREFIXES.some((prefix) => eventType.startsWith(prefix));
}

/** The props to send with a bridged event (PII filtering is lib/newrelic.ts's job). */
export function nrBridgeProps(props?: EventProps | null): EventProps {
  const out: EventProps = {};
  if (!props) return out;
  for (const [key, value] of Object.entries(props)) {
    if (!NR_BRIDGE_DROP_KEYS.has(key)) out[key] = value;
  }
  return out;
}
