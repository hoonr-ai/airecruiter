/**
 * New Relic Browser Agent integration for Curate Frontend (apps/web).
 *
 * How data reaches New Relic:
 *   initNewRelic()      -> starts the Browser agent, sets window.newrelic
 *   logStep()           -> window.newrelic.addPageAction (NR Browser > Custom events)
 *   captureException()  -> window.newrelic.noticeError   (NR Browser > JS Errors)
 *   Page load / AJAX    -> captured automatically by the agent
 */

let isInitialized = false;

export const initNewRelic = (): void => {
  if (isInitialized || typeof window === "undefined") return;

  // On PROD the copy/paste loader (public/newrelic-browser-agent.js) already
  // runs before hydration and sets window.NREUM/newrelic — never start a
  // second agent alongside it.
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  if ((window as any).NREUM?.loader_config) {
    isInitialized = true;
    return;
  }

  const licenseKey = process.env.NEXT_PUBLIC_NEW_RELIC_LICENSE_KEY;
  const accountID = process.env.NEXT_PUBLIC_NEW_RELIC_ACCOUNT_ID;
  const applicationID = process.env.NEXT_PUBLIC_NEW_RELIC_APPLICATION_ID;
  const agentID = process.env.NEXT_PUBLIC_NEW_RELIC_AGENT_ID || applicationID;
  const trustKey = process.env.NEXT_PUBLIC_NEW_RELIC_TRUST_KEY || accountID;

  if (!licenseKey || !accountID || !applicationID) {
    console.info(
      "[newrelic] Browser agent not started — set NEXT_PUBLIC_NEW_RELIC_LICENSE_KEY, " +
        "NEXT_PUBLIC_NEW_RELIC_ACCOUNT_ID and NEXT_PUBLIC_NEW_RELIC_APPLICATION_ID to enable."
    );
    return;
  }

  try {
    // Dynamic import inside client context so SSR does not fail
    import("@newrelic/browser-agent/loaders/browser-agent").then(({ BrowserAgent }) => {
      new BrowserAgent({
        init: {
          distributed_tracing: { enabled: true },
          privacy: { cookies_enabled: true },
          ajax: { deny_list: ["bam.nr-data.net"] },
        },
        info: {
          beacon: "bam.nr-data.net",
          errorBeacon: "bam.nr-data.net",
          licenseKey,
          applicationID,
          sa: 1,
        },
        loader_config: {
          accountID,
          trustKey,
          agentID,
          licenseKey,
          applicationID,
        },
      });

      isInitialized = true;
      console.log(
        `[newrelic] Browser agent started — account: ${accountID}, app: ${applicationID}`
      );
    }).catch((err) => {
      console.error("[newrelic] Failed to load browser agent module:", err);
    });
  } catch (error) {
    console.error("[newrelic] Failed to start browser agent:", error);
  }
};

/**
 * Record a named UI step in New Relic Browser as a Page Action custom event.
 */
export const logStep = (
  stepName: string,
  status: string,
  details?: Record<string, unknown>,
  category = "frontend_step"
): void => {
  const message = `Step '${stepName}': ${status}`;

  if (process.env.NODE_ENV === "development") {
    console.log(`[${category}] ${message}`, details ?? "");
  }

  if (status.toLowerCase() === "failed") {
    console.warn(`[newrelic] ${message}`, details ?? "");
  }

  if (typeof window === "undefined") return;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nr = (window as any).newrelic;
  if (!nr) return;

  try {
    nr.addPageAction(stepName, {
      status,
      category,
      ...(details ?? {}),
    });

    if (status.toLowerCase() === "failed") {
      nr.noticeError(new Error(`Step '${stepName}' failed`), {
        category,
        ...(details ?? {}),
      });
    }
  } catch (err) {
    console.debug("[newrelic] addPageAction failed:", err);
  }
};

/**
 * Report a frontend exception to New Relic Browser JS Errors.
 */
export const captureException = (
  error: unknown,
  context?: Record<string, unknown>
): void => {
  if (typeof window === "undefined") return;

  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const nr = (window as any).newrelic;
  if (!nr) return;

  try {
    nr.noticeError(
      error instanceof Error ? error : new Error(String(error)),
      context ?? {}
    );
  } catch (err) {
    console.debug("[newrelic] noticeError failed:", err);
  }
};
