"use client";

import * as Sentry from "@sentry/nextjs";
import { useEffect } from "react";

export default function GlobalError({
  error,
}: {
  error: Error & { digest?: string };
}) {
  useEffect(() => {
    Sentry.captureException(error);
  }, [error]);

  return (
    <html>
      <body
        style={{
          fontFamily: "system-ui, -apple-system, Segoe UI, Roboto, sans-serif",
          padding: "2rem",
          color: "#111",
        }}
      >
        <h2>Something went wrong</h2>
        <p style={{ color: "#555" }}>
          The issue has been reported. Please refresh the page or try again
          shortly.
        </p>
      </body>
    </html>
  );
}
