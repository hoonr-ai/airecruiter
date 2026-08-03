"use client";

import { useMsal } from "@azure/msal-react";
import { loginRequest } from "@/lib/msal-config";
import { LogIn, Bot } from "lucide-react";
import { useState } from "react";

export function LoginPage() {
    const { instance } = useMsal();
    const [isLoggingIn, setIsLoggingIn] = useState(false);

    const handleLogin = async () => {
        setIsLoggingIn(true);
        try {
            await instance.loginRedirect(loginRequest);
        } catch (e) {
            console.error(e);
            setIsLoggingIn(false);
        }
    };

    return (
        <div className="flex items-center justify-center bg-[#f8fafc] fixed inset-0 z-50 p-4 font-inter">
            <div className="w-full max-w-[440px] p-10 bg-white border border-slate-200 rounded-3xl shadow-xl shadow-slate-200/50">
                <div className="flex flex-col items-center text-center mb-10">
                    <div className="flex items-center justify-center mb-8">
                        {/* <Bot className="w-10 h-10 text-primary" strokeWidth={2.5} /> */}
                        {/* <span className="text-4xl font-bold tracking-tight text-primary font-outfit">PAIR</span> */}
                        {/* h-16, not h-28: the wordmark is 3.3:1, so 112px tall would
                            render ~370px wide and overflow this 360px card interior. */}
                        <img src="/pair-logo.png" alt="PAIR" className="h-16 w-auto object-contain" />
                    </div>
                    <h1 className="text-2xl font-semibold text-slate-900 tracking-tight">Sign in to your account</h1>
                </div>
                
                <button 
                    onClick={handleLogin}
                    disabled={isLoggingIn}
                    className="w-full flex justify-center items-center gap-3 px-6 py-3.5 bg-[#0078D4] hover:bg-[#006cbd] text-white rounded-xl transition-all duration-200 font-medium shadow-sm disabled:opacity-70 disabled:cursor-not-allowed hover:-translate-y-0.5"
                >
                    <LogIn className="w-5 h-5" />
                    <span className="text-[16px]">
                        {isLoggingIn ? "Signing in..." : "Sign in with Microsoft"}
                    </span>
                </button>
                
                <div className="mt-10 pt-6 border-t border-slate-100 text-center">
                    <p className="text-[13px] text-slate-400">
                        Secure, enterprise-grade authentication<br/>powered by Microsoft Entra ID.
                    </p>
                </div>
            </div>
        </div>
    );
}
