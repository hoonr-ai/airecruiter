"use client";

import { useEffect } from "react";
import { usePathname } from "next/navigation";
import { useMsal } from "@azure/msal-react";
import { identifyUser, initAnalytics, trackEvent } from "@/lib/analytics";

export function TelemetryBootstrap() {
  const pathname = usePathname();
  const { accounts } = useMsal();

  useEffect(() => {
    initAnalytics();
  }, []);

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
