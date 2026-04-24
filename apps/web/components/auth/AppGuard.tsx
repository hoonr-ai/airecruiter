"use client";

import { AuthenticatedTemplate, UnauthenticatedTemplate } from "@azure/msal-react";
import { LoginPage } from "@/components/auth/LoginPage";
import { ReactNode } from "react";

export function AppGuard({ children }: { children: ReactNode }) {
    return (
        <>
            <AuthenticatedTemplate>
                {children}
            </AuthenticatedTemplate>
            <UnauthenticatedTemplate>
                <LoginPage />
            </UnauthenticatedTemplate>
        </>
    );
}