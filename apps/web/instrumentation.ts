import * as Sentry from "@sentry/nextjs";

export async function register() {
  const dsn = process.env.NEXT_PUBLIC_SENTRY_DSN || process.env.SENTRY_DSN;
  if (!dsn) return;

  if (process.env.NEXT_RUNTIME === "nodejs") {
    Sentry.init({
      dsn,
      tracesSampleRate: 0.1,
      sendDefaultPii: false,
    });
  }

  if (process.env.NEXT_RUNTIME === "edge") {
    Sentry.init({
      dsn,
      tracesSampleRate: 0.1,
      sendDefaultPii: false,
    });
  }
}

// Forwards nested-RSC / Server-Action errors so Sentry sees them.
export const onRequestError = Sentry.captureRequestError;
