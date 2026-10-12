"use client";

import { ReactNode, useEffect, useState } from "react";
import { useMsal, useIsAuthenticated } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { LoginPage } from "./LoginPage";
import { ensureApiSession } from "@/lib/api";

// Local-development escape hatch. Set NEXT_PUBLIC_DISABLE_SSO=true in
// .env.local to skip the Microsoft Entra sign-in wall — useful when running
// without Azure credentials. Must remain unset/false in deployed envs; the
// flag is read at build time via Next's NEXT_PUBLIC_* inlining, so a prod
// build with the var absent can never bypass auth.
const SSO_DISABLED = process.env.NEXT_PUBLIC_DISABLE_SSO === "true";

// The session check (ensureApiSession) can sit on MSAL's hidden-iframe renewal
// for up to ~10s when the refresh token has expired. Past this, render the
// app anyway: its requests share the same token lookup, and the role badge
// waits for /auth/me rather than guessing.
const SESSION_CHECK_MAX_WAIT_MS = 15_000;
// A sign-in redirect leaves the page within moments. If it hasn't by then
// (MSAL refused to start it), render the app rather than spin forever.
const REDIRECT_MAX_WAIT_MS = 20_000;

type SessionState = "checking" | "ready" | "redirecting";

function FullScreenSpinner({ label }: { label: string }) {
    return (
        <div className="flex items-center justify-center fixed inset-0 bg-[#f8fafc] z-50">
            <div className="flex flex-col items-center gap-4">
                <div className="w-10 h-10 border-4 border-slate-200 border-t-primary rounded-full animate-spin" />
                <p className="text-sm text-slate-400 font-medium">{label}</p>
            </div>
        </div>
    );
}

export function AuthGuard({ children }: { children: ReactNode }) {
    const { inProgress } = useMsal();
    const isAuthenticated = useIsAuthenticated();
    // Latched: once MSAL has finished starting up (and handling a redirect
    // back from sign-in), later interactions never unmount the app.
    const [msalReady, setMsalReady] = useState(false);
    if (!msalReady && inProgress === InteractionStatus.None) setMsalReady(true);
    const [session, setSession] = useState<SessionState>("checking");

    // A cached account is not a usable session: after a long break its ID
    // token has expired, and rendering the app on it used to fire every
    // request into a 401 (the role fell back to "recruiter" until a late
    // loginRedirect reloaded the page). So get a token the API accepts
    // first, renewing it silently or going straight to sign-in, before any
    // page mounts.
    useEffect(() => {
        if (SSO_DISABLED || !msalReady || !isAuthenticated) return;
        let cancelled = false;
        const giveUp = window.setTimeout(() => {
            if (!cancelled) setSession((s) => (s === "checking" ? "ready" : s));
        }, SESSION_CHECK_MAX_WAIT_MS);
        ensureApiSession().then((outcome) => {
            if (cancelled) return;
            window.clearTimeout(giveUp);
            setSession(outcome === "redirecting" ? "redirecting" : "ready");
        });
        return () => {
            cancelled = true;
            window.clearTimeout(giveUp);
        };
    }, [msalReady, isAuthenticated]);

    useEffect(() => {
        if (session !== "redirecting") return;
        const stuck = window.setTimeout(() => setSession("ready"), REDIRECT_MAX_WAIT_MS);
        return () => window.clearTimeout(stuck);
    }, [session]);

    // Dev bypass: render the app without an MSAL session. TelemetryBootstrap
    // and AzureLoginButton both short-circuit when `accounts` is empty, so
    // downstream UI stays intact.
    if (SSO_DISABLED) {
        return <>{children}</>;
    }

    // MSAL is still starting up or handling the redirect back from sign-in.
    if (!msalReady) {
        return <FullScreenSpinner label="Authenticating..." />;
    }

    // Not authenticated → show the login page
    if (!isAuthenticated) {
        return <LoginPage />;
    }

    if (session === "checking") {
        return <FullScreenSpinner label="Restoring your session..." />;
    }
    if (session === "redirecting") {
        return <FullScreenSpinner label="Signing you in again..." />;
    }

    // Authenticated with a usable token → render the app
    return <>{children}</>;
}
