"use client";

import { nrBridgeProps, shouldBridgeToNr } from "@/lib/analytics-props";
import { nrPageAction } from "@/lib/newrelic";

type EventProps = Record<string, unknown>;

/**
 * Product event. Only the allowlisted ones (lib/analytics-props.ts) leave the
 * browser, as New Relic PageActions (PII-filtered there); every other name is
 * a no-op, so a call site costs nothing on the network. Page views, clicks,
 * AJAX timings and uncaught errors are recorded by the New Relic agent itself.
 * Never throws.
 */
export function trackEvent(eventType: string, props?: EventProps) {
  if (!shouldBridgeToNr(eventType)) return;
  try {
    nrPageAction(eventType, nrBridgeProps(props));
  } catch {
    // telemetry must never break the caller
  }
}
