import { Configuration, LogLevel, PublicClientApplication } from "@azure/msal-browser";

export const msalConfig: Configuration = {
    auth: {
        // 'Application (client) ID' of app registration in Azure portal - this value is a GUID
        clientId: process.env.NEXT_PUBLIC_AZURE_CLIENT_ID || "ENTER_THE_APPLICATION_ID_HERE",
        
        // Full directory URL, in the form of https://login.microsoftonline.com/<tenant-id>
        // Use 'common' for multi-tenant, or specific tenant ID
        authority: process.env.NEXT_PUBLIC_AZURE_AUTHORITY || "https://login.microsoftonline.com/common",
        
        // Must be the same as the redirect URI configured in Azure portal
        redirectUri: process.env.NEXT_PUBLIC_AZURE_REDIRECT_URI || (typeof window !== "undefined" ? window.location.origin : "/"),
    },
    cache: {
        cacheLocation: "sessionStorage", // This configures where your cache will be stored
        storeAuthStateInCookie: false, // Set this to "true" if you are having issues on IE11 or Edge
    },
    system: {
        loggerOptions: {
            loggerCallback: (level: any, message: any, containsPii: any) => {
                if (containsPii) {
                    return;
                }
                switch (level) {
                    case LogLevel.Error:
                        console.error(message);
                        return;
                    case LogLevel.Info:
                        console.info(message);
                        return;
                    case LogLevel.Verbose:
                        console.debug(message);
                        return;
                    case LogLevel.Warning:
                        console.warn(message);
                        return;
                }
            }
        }
    }
};

export const loginRequest = {
    scopes: ["User.Read"],
};

export const msalInstance = new PublicClientApplication(msalConfig);
