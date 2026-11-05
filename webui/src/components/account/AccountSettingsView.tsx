import { ChevronLeft } from "lucide-react";
import { useTranslation } from "react-i18next";

import { AccountPanel } from "@/components/account/AccountPanel";

/** The only settings screen a non-admin account sees: gateway-wide settings stay with administrators. */
export function AccountSettingsView({
  onBackToChat,
  onLogout,
}: {
  onBackToChat: () => void;
  onLogout: () => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="flex h-full w-full flex-col overflow-y-auto bg-background px-4 pb-12 pt-4 sm:px-8">
      <div className="mx-auto w-full max-w-2xl">
        <button
          type="button"
          onClick={onBackToChat}
          aria-label={t("settings.backToChat")}
          className="touch-target mb-4 inline-flex h-9 w-9 items-center justify-center rounded-xl text-sidebar-content hover:text-foreground"
        >
          <ChevronLeft className="h-4 w-4" aria-hidden />
        </button>
        <AccountPanel onLogout={onLogout} />
      </div>
    </div>
  );
}
