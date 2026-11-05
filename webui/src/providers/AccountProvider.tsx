import { createContext, useContext, type ReactNode } from "react";

export interface AccountContextValue {
  userId: string;
  isAdmin: boolean;
  /** Replaces the stored account token after a password change revoked the old one. */
  replaceToken: (token: string) => void;
}

const AccountContext = createContext<AccountContextValue | null>(null);

export function AccountProvider({
  value,
  children,
}: {
  value: AccountContextValue | null;
  children: ReactNode;
}) {
  return <AccountContext.Provider value={value}>{children}</AccountContext.Provider>;
}

/** The signed-in account, or null when the gateway is not in multi-user mode. */
export function useAccount(): AccountContextValue | null {
  return useContext(AccountContext);
}
