"use client";

import { useMsal } from "@azure/msal-react";
import { loginRequest } from "@/lib/msal-config";
import { LogOut, LogIn } from "lucide-react";

export function AzureLoginButton() {
    const { instance, accounts } = useMsal();

    if (process.env.NEXT_PUBLIC_HIDE_MICROSOFT_LOGIN === "true" && accounts.length === 0) {
        return null;
    }

    const handleLogin = () => {
        instance.loginPopup(loginRequest).catch((e) => {
            console.error(e);
        });
    };

    const handleLogout = () => {
        instance.logoutPopup({
            postLogoutRedirectUri: window.location.origin,
            mainWindowRedirectUri: window.location.origin,
        }).catch((e) => {
            console.error(e);
        });
    };

    if (accounts.length > 0) {
        return (
            <div className="flex flex-col gap-3 p-4 bg-slate-50 rounded-xl border border-slate-200 mt-auto">
                <div className="flex items-center gap-3">
                    <div className="w-8 h-8 rounded-full bg-primary/10 flex items-center justify-center text-primary font-bold">
                        {(accounts[0].name || accounts[0].username || "A").charAt(0).toUpperCase()}
                    </div>
                    <div className="flex flex-col overflow-hidden">
                        <span className="text-sm font-semibold truncate">{accounts[0].name}</span>
                        <span className="text-xs text-slate-500 truncate">{accounts[0].username}</span>
                    </div>
                </div>
                <button 
                    onClick={handleLogout}
                    className="flex w-full justify-center items-center gap-2 px-3 py-2 text-sm text-red-600 hover:bg-red-50 rounded-md transition-colors border border-transparent hover:border-red-100"
                >
                    <LogOut className="w-4 h-4" />
                    Sign Out
                </button>
            </div>
        );
    }

    return (
        <div className="mt-auto p-4">
            <button 
                onClick={handleLogin}
                className="flex w-full justify-center items-center gap-2 px-4 py-2.5 bg-[#0078D4] text-white rounded-lg hover:bg-[#006cbd] transition-colors font-medium shadow-sm"
            >
                <LogIn className="w-4 h-4" />
                Sign in with Microsoft
            </button>
        </div>
    );
}
