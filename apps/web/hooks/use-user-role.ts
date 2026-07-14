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

interface RoleData {
  role: "admin" | "recruiter";
  isAdmin: boolean;
}

const roleCache: Record<string, RoleData> = {};
const inflightRequests: Record<string, Promise<RoleData | null>> = {};

export function clearUserRoleCache(email?: string) {
  if (email) {
    delete roleCache[email];
    delete inflightRequests[email];
  } else {
    for (const key of Object.keys(roleCache)) delete roleCache[key];
    for (const key of Object.keys(inflightRequests)) delete inflightRequests[key];
  }
}

async function fetchRoleForEmail(email: string): Promise<RoleData | null> {
  try {
    const data = await api.auth.getMe();
    if (data) {
      const resolved: RoleData = {
        role: data.role === "admin" ? "admin" : "recruiter",
        isAdmin: data.role === "admin" || data.is_admin === true,
      };
      roleCache[email] = resolved;
      return resolved;
    }
  } catch (e) {
    console.error("Failed to fetch user role:", e);
  } finally {
    delete inflightRequests[email];
  }
  return null;
}

export function useUserRole(): UserRoleInfo {
  const { accounts } = useMsal();
  const email = accounts[0]?.username || getActiveUserEmail() || "";

  const [roleInfo, setRoleInfo] = useState<RoleData>(() => {
    if (email && roleCache[email]) {
      return roleCache[email];
    }
    return { role: "recruiter", isAdmin: false };
  });

  const [isLoading, setIsLoading] = useState<boolean>(() => {
    return !email || !roleCache[email];
  });

  useEffect(() => {
    if (!email) {
      setIsLoading(false);
      return;
    }

    if (roleCache[email]) {
      setRoleInfo(roleCache[email]);
      setIsLoading(false);
      return;
    }

    let isMounted = true;
    setIsLoading(true);

    if (!inflightRequests[email]) {
      inflightRequests[email] = fetchRoleForEmail(email);
    }

    inflightRequests[email].then((resolved) => {
      if (!isMounted) return;
      if (resolved) {
        setRoleInfo(resolved);
      }
      setIsLoading(false);
    });

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
