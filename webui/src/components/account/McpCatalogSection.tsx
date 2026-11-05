import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { accountErrorMessage } from "@/components/AccountLogin";
import { SettingsGroup, SettingsRow, SettingsSectionTitle } from "@/components/settings/shared/SettingsControls";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleButton } from "@/components/ui/toggle-button";
import {
  fetchMcpCatalog,
  setMcpCatalogEnabled,
  setMcpCatalogParams,
  type McpCatalogEntry,
} from "@/lib/account-api";

function ParamsForm({
  entry,
  token,
  onSaved,
}: {
  entry: McpCatalogEntry;
  token: string;
  onSaved: (entries: McpCatalogEntry[]) => void;
}) {
  const { t } = useTranslation();
  const [values, setValues] = useState<Record<string, string>>(() => Object.fromEntries(
    entry.params.map((p) => [p.name, p.secret ? "" : p.value ?? ""]),
  ));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      // Saving replaces the stored set; only filled fields are sent, secrets are never read back.
      const filled = Object.fromEntries(Object.entries(values).filter(([, v]) => v !== ""));
      onSaved(await setMcpCatalogParams(token, entry.name, filled));
    } catch (e) {
      setError(accountErrorMessage(e, t));
    } finally {
      setSaving(false);
    }
  };

  return (
    <form
      className="grid gap-2 px-3 pb-3"
      onSubmit={(e) => { e.preventDefault(); void save(); }}
    >
      {entry.params.map((param) => (
        <label key={param.name} className="grid gap-1 text-sm text-muted-foreground">
          <span>
            {param.name}
            {param.required ? " *" : ""}
            {param.description ? ` - ${param.description}` : ""}
          </span>
          <Input
            type={param.secret ? "password" : "text"}
            autoComplete="off"
            aria-label={`${entry.name} ${param.name}`}
            placeholder={param.secret && param.set ? t("app.account.mcp.secretSaved") : undefined}
            value={values[param.name] ?? ""}
            onChange={(e) => setValues((cur) => ({ ...cur, [param.name]: e.target.value }))}
          />
        </label>
      ))}
      <p className="text-xs text-muted-foreground">{t("app.account.mcp.replaceHint")}</p>
      {error ? <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}
      <div>
        <Button type="submit" size="sm" disabled={saving}>{t("app.account.save")}</Button>
      </div>
    </form>
  );
}

export function McpCatalogSection({ token }: { token: string }) {
  const { t } = useTranslation();
  const [entries, setEntries] = useState<McpCatalogEntry[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    fetchMcpCatalog(token).then((v) => { if (!cancelled) setEntries(v); })
      .catch((e) => { if (!cancelled) setError(accountErrorMessage(e, t)); });
    return () => { cancelled = true; };
  }, [token, t]);

  const toggle = async (name: string, enabled: boolean) => {
    setError(null);
    try {
      setEntries(await setMcpCatalogEnabled(token, name, enabled));
    } catch (e) {
      setError(accountErrorMessage(e, t));
    }
  };

  return (
    <section className="grid gap-3" aria-label={t("app.account.mcp.title")}>
      <SettingsSectionTitle>{t("app.account.mcp.title")}</SettingsSectionTitle>
      {error ? <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}
      {entries && entries.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t("app.account.mcp.empty")}</p>
      ) : null}
      {entries?.map((entry) => (
        <SettingsGroup key={entry.name}>
          <SettingsRow title={entry.name} description={entry.description || undefined}>
            <ToggleButton
              checked={entry.enabled}
              label={entry.name}
              onChange={(checked) => void toggle(entry.name, checked)}
            />
          </SettingsRow>
          {entry.params.length > 0 ? (
            <ParamsForm entry={entry} token={token} onSaved={setEntries} />
          ) : null}
        </SettingsGroup>
      ))}
    </section>
  );
}
