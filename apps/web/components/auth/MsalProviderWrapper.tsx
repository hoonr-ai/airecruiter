"use client";

import { MsalProvider } from "@azure/msal-react";
import { msalInstance } from "@/lib/msal-config";
import { ReactNode, useEffect, useState } from "react";

export function MsalProviderWrapper({ children }: { children: ReactNode }) {
    const [isInitialized, setIsInitialized] = useState(false);

    useEffect(() => {
        msalInstance.initialize().then(() => {
            setIsInitialized(true);
        }).catch(e => {
            console.error("MSAL init error", e);
        });
    }, []);

    if (!isInitialized) return null;

    return (
        <MsalProvider instance={msalInstance}>
            {children}
        </MsalProvider>
    );
}
