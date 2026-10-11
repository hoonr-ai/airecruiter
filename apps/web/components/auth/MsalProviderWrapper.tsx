"use client";

import { MsalProvider } from "@azure/msal-react";
import { msalInstance } from "@/lib/msal-config";
import { ReactNode, useEffect, useState } from "react";

export function MsalProviderWrapper({ children }: { children: ReactNode }) {
    const [isInitialized, setIsInitialized] = useState(false);

    useEffect(() => {
        msalInstance.initialize().then(() => {
            // Handle any pending redirect response (from loginRedirect / logoutRedirect)
            return msalInstance.handleRedirectPromise();
        }).then((response) => {
            // Never log the account object: it carries the user's name, email
            // and ID-token claims, and console output can reach telemetry.
            if (response) {
                console.log("SSO: redirect sign-in completed");
            } else {
                console.log("SSO: No redirect response (user already logged in or first visit)");
                const accounts = msalInstance.getAllAccounts();
                if (accounts.length > 0) {
                    console.log(`SSO: ${accounts.length} cached account(s) found`);
                }
            }
            setIsInitialized(true);
        }).catch(e => {
            console.error("MSAL init/redirect error:", e);
            // Still allow the app to render so the login page is visible
            setIsInitialized(true);
        });
    }, []);

    if (!isInitialized) return null;

    return (
        <MsalProvider instance={msalInstance}>
            {children}
        </MsalProvider>
    );
}
