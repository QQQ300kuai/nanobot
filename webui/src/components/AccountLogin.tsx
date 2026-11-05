import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { LanguageSwitcher } from "@/components/LanguageSwitcher";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { AccountApiError, loginAccount } from "@/lib/account-api";

export function accountErrorMessage(
  error: unknown,
  t: (key: string) => string,
): string {
  if (error instanceof AccountApiError) {
    if (error.code === "invalid_credentials") return t("app.account.invalidCredentials");
    if (error.code === "too_many_tickets") return t("app.account.tooManyRequests");
    return error.code;
  }
  return error instanceof Error ? error.message : String(error);
}

export function AccountLogin({
  expired = false,
  onToken,
}: {
  expired?: boolean;
  onToken: (token: string) => void;
}) {
  const { t } = useTranslation();
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(
    expired ? t("app.account.sessionExpired") : null,
  );

  const handleSubmit = async (event: FormEvent) => {
    event.preventDefault();
    if (!userId.trim() || !password) {
      setError(t("app.account.loginRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      onToken(await loginAccount(userId.trim(), password));
    } catch (e) {
      setError(accountErrorMessage(e, t));
      setSubmitting(false);
    }
  };

  return (
    <main className="flex h-full w-full flex-col overflow-y-auto bg-background">
      <div className="mx-5 mt-5 shrink-0 self-end sm:mx-8">
        <LanguageSwitcher className="h-8 min-w-0 gap-2 px-3 text-xs" />
      </div>
      <div className="flex flex-1 shrink-0 items-center justify-center px-6 pb-20 pt-8">
        <section aria-labelledby="webui-login-title" className="w-full max-w-xs">
          <div className="text-center">
            <img
              src="/brand/nanobot_mark.svg"
              alt=""
              width={56}
              height={56}
              draggable={false}
              className="mx-auto mb-5 h-14 w-14 select-none"
            />
            <h1
              id="webui-login-title"
              className="text-balance text-2xl font-semibold tracking-tight text-foreground sm:text-[1.75rem]"
            >
              {t("app.account.loginTitle")}
            </h1>
          </div>
          <form onSubmit={handleSubmit} className="mt-8 grid gap-3">
            <Input
              aria-label={t("app.account.userId")}
              placeholder={t("app.account.userId")}
              autoComplete="username"
              value={userId}
              onChange={(e) => setUserId(e.target.value)}
              disabled={submitting}
              className="h-12 rounded-full border-foreground/15 bg-muted/30 px-4 text-base"
              autoFocus
            />
            <Input
              aria-label={t("app.account.password")}
              placeholder={t("app.account.password")}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              disabled={submitting}
              className="h-12 rounded-full border-foreground/15 bg-muted/30 px-4 text-base"
            />
            {error ? (
              <p role="alert" className="px-2 text-center text-[13px] text-red-600 dark:text-red-400">
                {error}
              </p>
            ) : null}
            <Button type="submit" disabled={submitting} className="h-12 rounded-full">
              {t("app.account.signIn")}
            </Button>
          </form>
        </section>
      </div>
    </main>
  );
}
