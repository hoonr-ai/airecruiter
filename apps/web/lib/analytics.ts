"use client";

import * as amplitude from "@amplitude/analytics-browser";
import { nrBridgeProps, sanitizeEventProps, shouldBridgeToNr } from "@/lib/analytics-props";
import { nrPageAction } from "@/lib/newrelic";

type EventProps = Record<string, unknown>;

const AMPLITUDE_API_KEY = process.env.NEXT_PUBLIC_AMPLITUDE_API_KEY;
let initialized = false;

// Objects become JSON (≤ 1000 chars), not "[object Object]" — see analytics-props.ts.
const sanitize = sanitizeEventProps;

export function initAnalytics() {
  if (initialized || typeof window === "undefined" || !AMPLITUDE_API_KEY) return;
  amplitude.init(AMPLITUDE_API_KEY, {
    defaultTracking: false,
  });
  initialized = true;
}

export function identifyUser(userId?: string | null, userProperties?: EventProps) {
  if (!userId) return;
  initAnalytics();
  if (!initialized) return;
  amplitude.setUserId(userId);
  const traits = sanitize(userProperties);
  if (Object.keys(traits).length > 0) {
    const identifyObj = new amplitude.Identify();
    for (const [key, value] of Object.entries(traits)) {
      identifyObj.set(key, value as string | number | boolean);
    }
    amplitude.identify(identifyObj);
  }
}

export function trackEvent(eventType: string, props?: EventProps, userId?: string | null) {
  // Allowlisted product events also become New Relic PageActions (PII-filtered
  // there); independent of the Amplitude key, and never allowed to throw.
  if (shouldBridgeToNr(eventType)) {
    try {
      nrPageAction(eventType, nrBridgeProps(props));
    } catch {
      // telemetry must never break the caller
    }
  }
  initAnalytics();
  if (!initialized) return;
  if (userId) {
    amplitude.setUserId(userId);
  }
  amplitude.track(eventType, sanitize(props));
}
