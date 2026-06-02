import * as Sentry from "@sentry/nextjs";

const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN;

if (dsn) {
  Sentry.init({
    dsn,
    // 10% transaction sampling — errors are always sent at 100%.
    tracesSampleRate: 0.1,
    // Don't auto-attach PII (cookies, IPs). Flip to true if you need it.
    sendDefaultPii: false,
    // Spam guards
    ignoreErrors: [
      "ResizeObserver loop limit exceeded",
      "ResizeObserver loop completed with undelivered notifications.",
      "Non-Error promise rejection captured",
      // Common browser-extension / network noise
      "Network request failed",
      "Failed to fetch",
      "Load failed",
    ],
  });
}
