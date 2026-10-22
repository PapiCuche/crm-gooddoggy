"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";

import { useAuthTestAccounts } from "@/lib/api/client";

// Accesos de prueba (ADR-026 §5). Solo se pinta si el backend los tiene activos y hay cuentas:
// con el interruptor apagado (siempre en producción) la ruta da 404 y aquí no queda nada. Elegir
// una cuenta solo completa el correo; la contraseña se escribe como siempre.
export function TestAccounts({ onPick }: { onPick: (email: string) => void }) {
  const t = useTranslations("auth.testAccounts");
  const [picked, setPicked] = useState<string | null>(null);
  const list = useAuthTestAccounts({
    query: { retry: false, staleTime: Infinity, refetchOnWindowFocus: false },
  });
  const accounts = list.data?.accounts ?? [];
  if (accounts.length === 0) return null;
  return (
    <section
      aria-labelledby="test-accounts-title"
      className="border-border flex flex-col gap-3 border-t pt-4"
    >
      <div className="flex flex-col gap-1">
        <h2 id="test-accounts-title" className="text-muted text-[13px] tracking-wide uppercase">
          {t("title")}
        </h2>
        <p className="text-muted text-sm">{t("intro")}</p>
      </div>
      <ul className="flex flex-col gap-2">
        {accounts.map((account) => (
          <li key={account.email}>
            <button
              type="button"
              aria-pressed={picked === account.email}
              onClick={() => {
                setPicked(account.email);
                onPick(account.email);
              }}
              className="border-border hover:bg-surface-raised aria-pressed:border-foreground flex min-h-11 w-full flex-col items-start rounded-[10px] border px-3 py-2 text-left transition-[background-color,border-color,transform] duration-150 ease-out active:scale-[0.98] motion-reduce:transition-none motion-reduce:active:scale-100"
            >
              <span className="text-sm font-medium">{account.role}</span>
              <span className="text-muted font-mono text-[13px] wrap-anywhere">
                {account.email}
              </span>
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}
