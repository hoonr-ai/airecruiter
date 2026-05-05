"use client";

import { ReactNode, useEffect, useState } from "react";
import { useMsal, useIsAuthenticated } from "@azure/msal-react";
import { InteractionStatus } from "@azure/msal-browser";
import { LoginPage } from "./LoginPage";

export function AuthGuard({ children }: { children: ReactNode }) {
    const { inProgress } = useMsal();
    const isAuthenticated = useIsAuthenticated();
    const [isLoading, setIsLoading] = useState(true);

    useEffect(() => {
        // Wait until MSAL finishes any in-progress interaction
        // (e.g. handling the redirect response after login)
        if (inProgress === InteractionStatus.None) {
            setIsLoading(false);
        }
    }, [inProgress]);

    // Show a minimal loading state while MSAL processes the redirect
    if (isLoading) {
        return (
            <div className="flex items-center justify-center fixed inset-0 bg-[#f8fafc] z-50">
                <div className="flex flex-col items-center gap-4">
                    <div className="w-10 h-10 border-4 border-slate-200 border-t-primary rounded-full animate-spin" />
                    <p className="text-sm text-slate-400 font-medium">Authenticating...</p>
                </div>
            </div>
        );
    }

    // Not authenticated → show the login page
    if (!isAuthenticated) {
        return <LoginPage />;
    }

    // Authenticated → render the app
    return <>{children}</>;
}
