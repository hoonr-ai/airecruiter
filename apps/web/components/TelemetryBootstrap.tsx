"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { useMsal } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { identifyUser, initAnalytics, trackEvent } from "@/lib/analytics";
import { deriveAppEnv } from "@/lib/nr-config";
import { setNrAppVersion, setNrAttribute, setNrUser } from "@/lib/newrelic";

export function TelemetryBootstrap() {
  const pathname = usePathname();
  const { accounts, inProgress } = useMsal();
  const username = accounts?.[0]?.username;
  // No account only means "signed out" once MSAL has finished starting up.
  const signedOut = !username && inProgress === InteractionStatus.None;

  useEffect(() => {
    initAnalytics();
    // The New Relic agent itself is started by public/newrelic-browser-agent.js
    // (before hydration); here we only label its data.
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
    trackEvent("page_view", {
      path: pathname,
      title: typeof document !== "undefined" ? document.title : undefined,
    });
  }, [pathname]);

  useEffect(() => {
    const account = accounts?.[0];
    if (!account) return;
    identifyUser(account.username, {
      name: account.name,
      tenant_id: account.tenantId,
      home_account_id: account.homeAccountId,
    });
    trackEvent("user_authenticated", {
      path: pathname,
      tenant_id: account.tenantId,
    }, account.username);
  }, [accounts, pathname]);

  useEffect(() => {
    const onError = (event: ErrorEvent) => {
      // Amplitude only: the New Relic agent captures uncaught errors itself
      // (a noticeError here reported every one of them twice).
      trackEvent("frontend_error", {
        path: pathname,
        message: event.message,
        source: event.filename,
        line: event.lineno,
        column: event.colno,
      });
    };

    const onUnhandledRejection = (event: PromiseRejectionEvent) => {
      const reason = event.reason;
      // Amplitude only, as above (the agent captures unhandled rejections).
      trackEvent("frontend_unhandled_rejection", {
        path: pathname,
        reason: reason?.message || String(reason),
      });
    };

    window.addEventListener("error", onError);
    window.addEventListener("unhandledrejection", onUnhandledRejection);

    return () => {
      window.removeEventListener("error", onError);
      window.removeEventListener("unhandledrejection", onUnhandledRejection);
    };
  }, [pathname]);

  useEffect(() => {
    const onClick = (event: MouseEvent) => {
      const target = event.target as HTMLElement | null;
      const element = target?.closest("button, a, [role='button']") as HTMLElement | null;
      if (!element) return;

      const tag = element.tagName.toLowerCase();
      const href = tag === "a" ? element.getAttribute("href") : null;
      trackEvent("ui_click", {
        path: pathname,
        tag,
        id: element.id || undefined,
        role: element.getAttribute("role") || undefined,
        aria_label: element.getAttribute("aria-label") || undefined,
        data_track: element.getAttribute("data-track") || undefined,
        href: href || undefined,
      });
    };

    window.addEventListener("click", onClick, { capture: true });
    return () => {
      window.removeEventListener("click", onClick, { capture: true });
    };
  }, [pathname]);

  return null;
}
