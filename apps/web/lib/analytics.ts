"use client";

import * as amplitude from "@amplitude/analytics-browser";

type EventProps = Record<string, unknown>;

const AMPLITUDE_API_KEY = process.env.NEXT_PUBLIC_AMPLITUDE_API_KEY;
let initialized = false;

function sanitize(props?: EventProps): EventProps {
  if (!props) return {};
  const out: EventProps = {};
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined) continue;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      out[key] = value;
      continue;
    }
    out[key] = String(value);
  }
  return out;
}

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
  initAnalytics();
  if (!initialized) return;
  if (userId) {
    amplitude.setUserId(userId);
  }
  amplitude.track(eventType, sanitize(props));
}
