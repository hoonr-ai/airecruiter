"use client";

import { useEffect, useState } from "react";
import { useMsal } from "@azure/msal-react";
import { api, getActiveUserEmail } from "@/lib/api";

export type UserRole = "admin" | "team_lead" | "recruiter";

export interface UserRoleInfo {
  email: string;
  role: UserRole;
  isAdmin: boolean;
  isTeamLead: boolean;
  teamId: string | null;
  teamName: string | null;
  /** Level in the org hierarchy ("resource_manager", "avp", ...); null when not in it. */
  orgRole: string | null;
  /** Display name of that level ("Resource Manager", "AVP"); use it instead of "Team Lead". */
  orgRoleLabel: string | null;
  /** Someone reports to them — the server gives them analytics scoped to everyone beneath. */
  managesPeople: boolean;
  isLoading: boolean;
}

interface RoleData {
  role: UserRole;
  isAdmin: boolean;
  isTeamLead: boolean;
  teamId: string | null;
  teamName: string | null;
  orgRole: string | null;
  orgRoleLabel: string | null;
  managesPeople: boolean;
}

const DEFAULT_ROLE: RoleData = {
  role: "recruiter",
  isAdmin: false,
  isTeamLead: false,
  teamId: null,
  teamName: null,
  orgRole: null,
  orgRoleLabel: null,
  managesPeople: false,
};

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
      const role: UserRole =
        data.role === "admin" ? "admin" : data.role === "team_lead" ? "team_lead" : "recruiter";
      const resolved: RoleData = {
        role,
        isAdmin: role === "admin" || data.is_admin === true,
        isTeamLead: role === "team_lead" || data.is_team_lead === true,
        teamId: data.team_id || null,
        teamName: data.team_name || null,
        orgRole: data.org_role || null,
        orgRoleLabel: data.org_role_label || null,
        managesPeople: data.manages_people === true,
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
    return DEFAULT_ROLE;
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
    isTeamLead: roleInfo.isTeamLead,
    teamId: roleInfo.teamId,
    teamName: roleInfo.teamName,
    orgRole: roleInfo.orgRole,
    orgRoleLabel: roleInfo.orgRoleLabel,
    managesPeople: roleInfo.managesPeople,
    isLoading,
  };
}
