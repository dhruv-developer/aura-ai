"use client";

import { auth, type AccountAccess, type User } from "@aura/api-client";
import { createContext, useContext } from "react";

export interface PersonalState {
  user: User;
  account: AccountAccess; // the signed-in user's personal account
  organizations: AccountAccess[];
}

export const PersonalContext = createContext<PersonalState | null>(null);

export function usePersonal(): PersonalState {
  const state = useContext(PersonalContext);
  if (!state) throw new Error("usePersonal must be used inside the /app layout");
  return state;
}

export async function loadPersonal(): Promise<PersonalState> {
  const me = await auth.me();
  const account = me.accounts.find((a) => a.kind === "individual");
  if (!account) throw new Error("No personal account found");
  return { user: me.user, account, organizations: me.accounts.filter((a) => a.kind === "organization") };
}
