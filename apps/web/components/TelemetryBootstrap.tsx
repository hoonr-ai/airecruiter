"use client";

import { useEffect } from "react";
import { useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { deriveAppEnv } from "@/lib/nr-config";
import { setNrAppVersion, setNrAttribute, setNrUser } from "@/lib/newrelic";
import { startUserSession } from "@/lib/user-session";

// Labels the New Relic Browser data. The agent itself is started by
// public/newrelic-browser-agent.js (before hydration) and records page views,
// route changes, clicks, AJAX and uncaught errors on its own, so nothing here
// listens for them or sends anything.
export function TelemetryBootstrap() {
  const { accounts, inProgress } = useMsal();
  const username = accounts?.[0]?.username;
  const displayName = accounts?.[0]?.name;
  // No account only means "signed out" once MSAL has finished starting up.
  const signedOut = !username && inProgress === InteractionStatus.None;

  useEffect(() => {
    setNrAttribute("app_env", process.env.NEXT_PUBLIC_APP_ENV || deriveAppEnv(window.location.hostname));
    setNrAppVersion(process.env.NEXT_PUBLIC_APP_VERSION);
  }, []);

  useEffect(() => {
    // NR gets a pseudonymous id (sha256 of the email, 12 hex), never the email.
    // Signed out clears it: the agent keeps the id in its session, so the
    // previous recruiter would otherwise own every event until the next
    // sign-in. Not while MSAL is still loading the account, so a page load
    // does not drop the id for a moment.
    if (username || signedOut) void setNrUser(username).catch(() => {});
  }, [username, signedOut]);

  useEffect(() => {
    // Login time / device / time spent (lib/user-session.ts).
    if (username) startUserSession(username, displayName || undefined);
  }, [username, displayName]);

  return null;
}
