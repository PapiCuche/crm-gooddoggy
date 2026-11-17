"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Unplug } from "lucide-react";
import { useFormatter, useTranslations } from "next-intl";
import { type ReactNode, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { StepUpDialog } from "@/components/security/step-up-dialog";
import { STEP_UP_REQUIRED } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import {
  aiProviderAccountsCreate,
  aiProviderAccountsDestroy,
  aiProviderAccountsList,
  aiProviderAccountsRotateKey,
  aiProviderAccountsTest,
  getAiProviderAccountsListQueryKey,
} from "@/lib/api/client";
import { type AiProviderAccount, AiProviderEnum, AiProviderTestResultEnum } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { AccountAction, aiError, KeyForm } from "./ai-key-actions";

const MANAGE = "ai.manage";
const STATUS = {
  ACTIVE: { Icon: Check, tone: "text-success" },
  DISABLED: { Icon: Unplug, tone: "text-muted" },
} as const;

// El proveedor de IA (F8-04): las cuentas de `…/ai/provider-accounts/` (F8-03c/d), guardar o rotar
// la clave, borrarla y probar la conexión. Todas las rutas piden `ai.manage` y el segundo factor
// reciente, y comparten un tope por persona que cuenta también las lecturas: la lista se pide una
// vez, sin reintentos ni al recuperar el foco. Guardar y rotar actualizan la caché con lo que
// devuelven; borrar (o un 404) marca la cuenta como borrada en la caché y vuelve a leer la lista
// una vez. De la clave solo se enseñan los últimos cuatro caracteres que manda la API.
export function AiProviderAccounts() {
  const t = useTranslations("ai");
  const errors = useTranslations("errors");
  const own = useTranslations("ai.errors");
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const allowed = permissions.some((grant) => grant.code === MANAGE);
  const queryClient = useQueryClient();
  const key = getAiProviderAccountsListQueryKey(slug);
  const list = useQuery<AiProviderAccount[], ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => aiProviderAccountsList(slug, { signal }),
    enabled: allowed, // sin el permiso no se gasta el tope en un 403
    ...{ staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false },
    retry: false,
  });
  const heading = useRef<HTMLHeadingElement>(null);
  const [said, setSaid] = useState<{ text: string; alert?: boolean } | null>(null);
  const [dismissed, setDismissed] = useState(false); // el código de la lista, cancelado
  const reload = () => void list.refetch();
  const put = (account: AiProviderAccount) => {
    queryClient.setQueryData<AiProviderAccount[]>(key, (rows = []) =>
      rows.some((row) => row.id === account.id)
        ? rows.map((row) => (row.id === account.id ? account : row))
        : [...rows, account],
    );
  };
  // La cuenta ya no está activa (se borró, o un 404: otra persona la borró). Se marca en la caché
  // antes de volver a leer: si la lectura falla, la tarjeta no sigue «Activa» con sus acciones. Sus
  // acciones se desmontan, así que lo que pasó lo dice la página y el foco va al título.
  const gone = (account: AiProviderAccount, text: string, alert?: boolean) => {
    put({ ...account, status: "DISABLED" });
    setSaid({ text, alert });
    setTimeout(() => heading.current?.focus());
    reload();
  };
  const ctx: Ctx = {
    ...{ slug, put },
    removed: (account) => gone(account, t("remove.done")),
    lost: (account) => gone(account, own("NOT_FOUND"), true),
  };
  // Lo que no se puede arreglar en el formulario (ya existe o ya no está): se vuelve a leer.
  const stale = (error: ApiError) =>
    (error.status === 404 || error.code === "AI_ACCOUNT_EXISTS") && reload();

  let body: ReactNode;
  if (!allowed || list.error?.code === "PERMISSION_DENIED") {
    body = <p role="alert">{t("denied")}</p>;
  } else if (list.data) {
    const rows = list.data;
    const active = rows.some((row) => row.status === "ACTIVE");
    body = (
      <div className="flex flex-col gap-4">
        {active ? null : (
          <KeyForm
            mode="save"
            send={(apiKey) =>
              aiProviderAccountsCreate(slug, { provider: "ANTHROPIC", api_key: apiKey })
            }
            onDone={(account) => {
              put(account); // el formulario se va con la cuenta activa: lo dice la página
              setSaid({ text: t("save.done") });
              setTimeout(() => heading.current?.focus());
            }}
            onFailed={stale}
          />
        )}
        {rows.length ? (
          <ul aria-label={t("title")} className="flex flex-col gap-3">
            {rows.map((account) => (
              <Account key={account.id} account={account} ctx={ctx} />
            ))}
          </ul>
        ) : (
          <p className="text-muted">{t("empty")}</p>
        )}
      </div>
    );
  } else if (list.isError && list.error.status !== 401) {
    // Sin el segundo factor reciente (también para leer), el código se pide al llegar y se puede
    // volver a abrir; cualquier otro error, con un reintento a mano: el tope cuenta cada lectura.
    const step = list.error.code === STEP_UP_REQUIRED;
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className={step ? undefined : "text-danger"}>
          {step
            ? t("stepUp")
            : list.error.code === "NOT_FOUND"
              ? t("missing")
              : (aiError(list.error, own)?.text ?? errors(`api.${apiErrorKey(list.error)}`))}
        </p>
        <Button variant="primary" size="lg" onClick={step ? () => setDismissed(false) : reload}>
          {step ? t("verify") : errors("retry")}
        </Button>
        {step && !dismissed && !list.isFetching ? (
          <StepUpDialog
            onVerified={() => {
              setDismissed(true);
              void list.refetch().finally(() => setDismissed(false));
            }}
            onCancel={() => setDismissed(true)}
          />
        ) : null}
      </div>
    );
  } else {
    body = (
      <p role="status" className="text-muted">
        {t("loading")}
      </p>
    );
  }

  return (
    <section className="flex max-w-4xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{t("eyebrow")}</p>
        <h1
          ref={heading}
          tabIndex={-1}
          className="text-[28px] leading-tight font-bold tracking-[0.02em] focus-visible:outline-none"
        >
          {t("title")}
        </h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      <p role="status" className={said && !said.alert ? "text-sm" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm">
          {said.text}
        </p>
      ) : null}
      {body}
    </section>
  );
}

type Ctx = {
  slug: string;
  put: (account: AiProviderAccount) => void;
  removed: (account: AiProviderAccount) => void;
  lost: (account: AiProviderAccount) => void; // 404: la cuenta ya no está
};

function Account({
  account,
  ctx: { slug, put, removed, lost },
}: {
  account: AiProviderAccount;
  ctx: Ctx;
}) {
  const t = useTranslations("ai");
  const format = useFormatter();
  // Un código que esta versión no conoce (la API puede crecer antes) se dice como desconocido.
  const label = (group: string, known: object, value: string) =>
    t(`${group}.${Object.hasOwn(known, value) ? value : "UNKNOWN"}` as never);
  const provider = label("provider", AiProviderEnum, account.provider);
  const { Icon, tone } = Object.hasOwn(STATUS, account.status)
    ? STATUS[account.status]
    : STATUS.DISABLED;
  const when = (value: string) =>
    format.dateTime(new Date(value), {
      ...{ day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" },
    });
  const dates = [
    ["configured", account.configured_at],
    ["rotated", account.rotated_at],
    ["revoked", account.revoked_at],
  ] as const;

  return (
    <li className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4">
      <div className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1">
        <h2 className="font-medium">{provider}</h2>
        <p className="flex items-center gap-1.5 text-sm font-medium">
          <Icon aria-hidden className={cn("size-4 shrink-0", tone)} />
          {label("status", STATUS, account.status)}
        </p>
      </div>
      <dl className="grid gap-x-4 gap-y-1 text-sm sm:grid-cols-[auto_minmax(0,1fr)]">
        <dt className="text-muted">{t("fields.key")}</dt>
        <dd className="font-mono">…{account.key_last_four}</dd>
        {dates.map(([name, value]) =>
          value ? (
            <div key={name} className="contents">
              <dt className="text-muted">{t(`fields.${name}`)}</dt>
              <dd>
                <time dateTime={value}>{when(value)}</time>
              </dd>
            </div>
          ) : null,
        )}
      </dl>
      {account.status === "ACTIVE" ? (
        <div className="flex flex-col gap-3">
          <AccountAction
            label={t("test.open")}
            busyLabel={t("test.busy")}
            run={() => aiProviderAccountsTest(slug, account.id)}
            // Solo el código que devuelve la API, traducido; uno desconocido no se pinta.
            done={({ result }) => ({
              ...{ text: label("test", AiProviderTestResultEnum, result), ok: result === "OK" },
            })}
            onGone={() => lost(account)}
          />
          <KeyForm
            mode="rotate"
            send={(apiKey) => aiProviderAccountsRotateKey(slug, account.id, { api_key: apiKey })}
            onDone={put}
            onFailed={(error) => error.status === 404 && lost(account)}
          />
          <AccountAction
            label={t("remove.open")}
            busyLabel={t("remove.busy")}
            run={() => aiProviderAccountsDestroy(slug, account.id)}
            done={() => (removed(account), null)}
            onGone={() => lost(account)}
            confirm={{
              ...{ title: t("remove.title", { provider }), effect: t("remove.effect") },
              ...{ question: t("remove.question", { provider }), yes: t("remove.confirm") },
              no: t("remove.cancel"),
            }}
          />
        </div>
      ) : null}
    </li>
  );
}
