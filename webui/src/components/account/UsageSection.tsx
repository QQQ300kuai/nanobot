import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { accountErrorMessage } from "@/components/AccountLogin";
import { ReadOnlyRow, SettingsGroup, SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { fetchAdminUsage, fetchOwnUsage, type UsageSummary } from "@/lib/account-api";
import { formatCompactTokenCount } from "@/lib/format";

export function UsageSection({ token, isAdmin }: { token: string; isAdmin: boolean }) {
  const { t } = useTranslation();
  const [own, setOwn] = useState<UsageSummary | null>(null);
  const [all, setAll] = useState<UsageSummary | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    fetchOwnUsage(token).then((v) => { if (!cancelled) setOwn(v); })
      .catch((e) => { if (!cancelled) setError(accountErrorMessage(e, t)); });
    if (isAdmin) {
      fetchAdminUsage(token).then((v) => { if (!cancelled) setAll(v); })
        .catch((e) => { if (!cancelled) setError(accountErrorMessage(e, t)); });
    }
    return () => { cancelled = true; };
  }, [token, isAdmin, t]);

  return (
    <section className="grid gap-3" aria-label={t("app.account.usage.title")}>
      <SettingsSectionTitle>{t("app.account.usage.title")}</SettingsSectionTitle>
      {error ? <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}
      {own ? (
        <SettingsGroup>
          <ReadOnlyRow title={t("app.account.usage.tokens")} value={formatCompactTokenCount(own.total_tokens_30d)} />
          <ReadOnlyRow title={t("app.account.usage.requests")} value={String(own.requests_30d)} />
          <ReadOnlyRow title={t("app.account.usage.failed")} value={String(own.failed_requests_30d)} />
          {own.providers_30d.map((row) => (
            <ReadOnlyRow
              key={`${row.provider}/${row.model}`}
              title={`${row.provider} / ${row.model}`}
              value={formatCompactTokenCount(row.total_tokens)}
            />
          ))}
        </SettingsGroup>
      ) : null}
      {isAdmin && all?.users_30d ? (
        <>
          <SettingsSectionTitle>{t("app.account.usage.perUser")}</SettingsSectionTitle>
          <SettingsGroup>
            {all.users_30d.map((row) => (
              <ReadOnlyRow
                key={row.user_id ?? "unowned"}
                title={row.user_id ?? t("app.account.usage.unowned")}
                value={t("app.account.usage.userSummary", {
                  tokens: formatCompactTokenCount(row.total_tokens),
                  requests: row.requests,
                })}
              />
            ))}
          </SettingsGroup>
        </>
      ) : null}
    </section>
  );
}
