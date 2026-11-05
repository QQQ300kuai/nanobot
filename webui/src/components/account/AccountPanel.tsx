import { LogOut } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { ChangePasswordDialog } from "@/components/account/ChangePasswordDialog";
import { McpCatalogSection } from "@/components/account/McpCatalogSection";
import { UsageSection } from "@/components/account/UsageSection";
import { ReadOnlyRow, SettingsGroup, SettingsRow, SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import { useClient } from "@/providers/ClientProvider";
import { useAccount } from "@/providers/AccountProvider";

/** Identity, password, usage and the MCP catalog of the signed-in account. */
export function AccountPanel({ onLogout }: { onLogout?: () => void }) {
  const { t } = useTranslation();
  const account = useAccount();
  const { token } = useClient();
  const [passwordOpen, setPasswordOpen] = useState(false);
  if (!account) return null;

  return (
    <div className="grid gap-8">
      <section className="grid gap-3" aria-label={t("app.account.section")}>
        <SettingsSectionTitle>{t("app.account.section")}</SettingsSectionTitle>
        <SettingsGroup>
          <ReadOnlyRow title={t("app.account.userId")} value={account.userId} />
          <SettingsRow title={t("app.account.changePassword")}>
            <Button type="button" variant="outline" size="sm" onClick={() => setPasswordOpen(true)}>
              {t("app.account.changePassword")}
            </Button>
          </SettingsRow>
          {onLogout ? (
            <SettingsRow title={t("app.account.logoutHint")}>
              <Button type="button" variant="outline" size="sm" onClick={onLogout}>
                <LogOut className="mr-1 h-4 w-4" aria-hidden />
                {t("app.account.logout")}
              </Button>
            </SettingsRow>
          ) : null}
        </SettingsGroup>
      </section>
      <UsageSection token={token} isAdmin={account.isAdmin} />
      <McpCatalogSection token={token} />
      <ChangePasswordDialog
        open={passwordOpen}
        token={token}
        onClose={() => setPasswordOpen(false)}
        onChanged={account.replaceToken}
      />
    </div>
  );
}
