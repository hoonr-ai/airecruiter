"use client";

import { useEffect, useState } from "react";
import { useMsal } from "@azure/msal-react";
import { api, getActiveUserEmail } from "@/lib/api";

export interface UserRoleInfo {
  email: string;
  role: "admin" | "recruiter";
  isAdmin: boolean;
  isLoading: boolean;
}

export function useUserRole(): UserRoleInfo {
  const { accounts } = useMsal();
  const email = accounts[0]?.username || getActiveUserEmail() || "";
  
  const [roleInfo, setRoleInfo] = useState<{ role: "admin" | "recruiter"; isAdmin: boolean }>({
    role: "recruiter",
    isAdmin: false,
  });
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let isMounted = true;
    const fetchRole = async () => {
      setIsLoading(true);
      try {
        const data = await api.auth.getMe();
        if (isMounted && data) {
          setRoleInfo({
            role: data.role === "admin" ? "admin" : "recruiter",
            isAdmin: data.role === "admin" || data.is_admin === true,
          });
        }
      } catch (e) {
        console.error("Failed to fetch user role:", e);
      } finally {
        if (isMounted) setIsLoading(false);
      }
    };

    fetchRole();
    return () => {
      isMounted = false;
    };
  }, [email]);

  return {
    email,
    role: roleInfo.role,
    isAdmin: roleInfo.isAdmin,
    isLoading,
  };
}
