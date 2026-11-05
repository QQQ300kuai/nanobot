import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";

import { accountErrorMessage } from "@/components/AccountLogin";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { changeAccountPassword } from "@/lib/account-api";

export function ChangePasswordDialog({
  open,
  token,
  onClose,
  onChanged,
}: {
  open: boolean;
  token: string;
  onClose: () => void;
  onChanged: (token: string) => void;
}) {
  const { t } = useTranslation();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const reset = () => {
    setCurrent("");
    setNext("");
    setError(null);
    setSubmitting(false);
  };

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!current || !next) {
      setError(t("app.account.passwordRequired"));
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      onChanged(await changeAccountPassword(token, current, next));
      reset();
      onClose();
    } catch (e) {
      setError(accountErrorMessage(e, t));
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={(value) => { if (!value) { reset(); onClose(); } }}>
      <DialogContent className="max-w-sm">
        <form className="grid gap-4" onSubmit={submit}>
          <DialogHeader className="text-left">
            <DialogTitle>{t("app.account.changePassword")}</DialogTitle>
            <DialogDescription>{t("app.account.changePasswordHint")}</DialogDescription>
          </DialogHeader>
          <Input
            type="password"
            aria-label={t("app.account.currentPassword")}
            placeholder={t("app.account.currentPassword")}
            autoComplete="current-password"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            disabled={submitting}
            autoFocus
          />
          <Input
            type="password"
            aria-label={t("app.account.newPassword")}
            placeholder={t("app.account.newPassword")}
            autoComplete="new-password"
            value={next}
            onChange={(e) => setNext(e.target.value)}
            disabled={submitting}
          />
          {error ? <p role="alert" className="text-sm text-red-600 dark:text-red-400">{error}</p> : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => { reset(); onClose(); }}>
              {t("app.account.cancel")}
            </Button>
            <Button type="submit" disabled={submitting}>{t("app.account.save")}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
