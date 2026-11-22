"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { StepUpDialog } from "@/components/security/step-up-dialog";
import { isStepUpCancelled, STEP_UP_REQUIRED, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { aiBudgetRetrieve, aiBudgetUpdate, getAiBudgetRetrieveQueryKey } from "@/lib/api/client";
import type { AiBudget } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { aiError, toLogin } from "./ai-key-actions";
import { MANAGE } from "./ai-switch";

const NAMES = ["monthly_usd", "daily_usd"] as const;
type Name = (typeof NAMES)[number];
type Problem = "REQUIRED" | "FORMAT" | "DECIMALS" | "TOO_BIG" | "ZERO" | "ABOVE" | "INVALID";
type Problems = Partial<Record<Name, Problem>>;
type Said = { text: string; alert?: boolean } | null;
const PLACES = 6; // `numeric(12, 6)` del backend: 6 decimales y menos de un millón
const AMOUNT = /^(\d+)(?:\.(\d+))?$/; // con punto: «1,000» no se adivina (¿mil o uno?)
// Los códigos de DRF en un 400 de cada importe; cualquier otro se dice como `INVALID`.
const SERVER: Record<string, Problem> = {
  ...{ required: "REQUIRED", null: "REQUIRED", min_value: "ZERO" },
  ...{ max_decimal_places: "DECIMALS", max_whole_digits: "TOO_BIG", max_digits: "TOO_BIG" },
};

// Millonésimas de dólar de un importe ya validado: se compara sin redondeos de coma flotante.
function micros(value: string) {
  const [, whole = "0", fraction = ""] = AMOUNT.exec(value) ?? [];
  return BigInt(whole) * 1_000_000n + BigInt(fraction.padEnd(PLACES, "0"));
}

// Lo que la API rechazaría de un importe, comprobado antes de enviar (la API sigue decidiendo).
function problem(value: string): Problem | null {
  const found = AMOUNT.exec(value);
  if (!value) return "REQUIRED";
  if (!found) return "FORMAT";
  if ((found[2] ?? "").length > PLACES) return "DECIMALS";
  if (found[1]!.replace(/^0+/, "").length > PLACES) return "TOO_BIG";
  return micros(value) === 0n ? "ZERO" : null;
}

// Los importes que la API no aceptó, por su `code`: el texto del servidor nunca se pinta.
function rejected(error: ApiError): Problems | null {
  if (error.status !== 400 || error.code !== "VALIDATION_ERROR") return null;
  const out: Problems = {};
  for (const name of NAMES) {
    const found = error.fields?.[name];
    if (!Array.isArray(found)) continue;
    const code: unknown = (found[0] as { code?: unknown } | null)?.code;
    out[name] = typeof code === "string" && Object.hasOwn(SERVER, code) ? SERVER[code] : "INVALID";
  }
  return Object.keys(out).length ? out : null;
}

// «50.000000» → «50»: el mismo importe, como se escribiría.
const plain = (value: string | null | undefined) =>
  !value ? "" : value.includes(".") ? value.replace(/0+$/, "").replace(/\.$/, "") : value;

// Los topes de gasto de la IA (F8-12a): `…/ai/budget/` (F8-05b3). Leerlos y fijarlos piden
// `ai.manage` y el segundo factor reciente; guardar cuenta en el tope por persona de las
// escrituras (429) y queda en la auditoría (`ai.limits.changed`). Los importes son USD con 6
// decimales, el día y el mes de Lima. La API no devuelve lo gastado: no se inventa aquí.
export function AiBudgetPanel() {
  const t = useTranslations("ai.budget");
  const ai = useTranslations("ai");
  const own = useTranslations("ai.errors");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const allowed = permissions.some((grant) => grant.code === MANAGE);
  const queryClient = useQueryClient();
  const key = getAiBudgetRetrieveQueryKey(slug);
  const query = useQuery<AiBudget, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => aiBudgetRetrieve(slug, { signal }),
    enabled: allowed, // sin el permiso no se pide: sería un 403
    ...{ staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false },
    retry: false,
  });
  const { withStepUp, dialog } = useStepUp();
  const [draft, setDraft] = useState<Partial<Record<Name, string>>>({}); // sin la clave: lo guardado
  const [found, setFound] = useState<Problems>({});
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false); // el código de la lectura, cancelado
  const sending = useRef(false); // dos pulsaciones seguidas no son dos escrituras
  const inputs = useRef<Partial<Record<Name, HTMLInputElement | null>>>({});
  const id = useId();

  const data = query.data;
  const value = (name: Name) => draft[name] ?? plain(data?.[name]);
  const usd = (amount: string) =>
    format.number(Number(amount), {
      ...{ style: "currency", currency: "USD" },
      ...{ minimumFractionDigits: 2, maximumFractionDigits: PLACES },
    });
  const fail = (problems: Problems) => {
    setFound(problems);
    inputs.current[NAMES.find((name) => problems[name])!]?.focus(); // al primero que falla
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    if (sending.current || !data) return;
    const wanted = {
      monthly_usd: value("monthly_usd").trim(),
      daily_usd: value("daily_usd").trim(),
    };
    setSaid(null);
    const problems: Problems = {};
    for (const name of NAMES) {
      const bad = problem(wanted[name]);
      if (bad) problems[name] = bad;
    }
    if (!problems.monthly_usd && !problems.daily_usd) {
      if (micros(wanted.daily_usd) > micros(wanted.monthly_usd)) problems.daily_usd = "ABOVE";
    }
    if (Object.keys(problems).length) return fail(problems);
    setFound({});
    const same = (name: Name) => data[name] !== null && micros(data[name]) === micros(wanted[name]);
    if (NAMES.every(same)) return setSaid({ text: t("unchanged") });
    sending.current = true;
    setBusy(true);
    try {
      const saved = await withStepUp(() => aiBudgetUpdate(slug, wanted));
      queryClient.setQueryData(key, saved);
      setDraft({});
      setSaid({ text: t("saved") });
    } catch (caught) {
      const error = caught as ApiError;
      const cancelled: boolean = isStepUpCancelled(error); // cancelar el código no es un fallo
      if (cancelled || toLogin(error)) return;
      const problems = rejected(error);
      if (problems) return fail(problems);
      // De `aiError`, solo el tope y el código: su `NOT_FOUND` habla de la clave del proveedor.
      const fromAi = error.code === "RATE_LIMITED" || error.code === STEP_UP_REQUIRED;
      const text =
        error.code === "PERMISSION_DENIED"
          ? own("denied")
          : ((fromAi ? aiError(error, own)?.text : null) ?? errors(`api.${apiErrorKey(error)}`));
      setSaid({ text, alert: true });
    } finally {
      sending.current = false;
      setBusy(false);
    }
  }

  let body: ReactNode;
  if (!allowed || query.error?.code === "PERMISSION_DENIED") {
    body = <p role="alert">{ai("denied")}</p>;
  } else if (data) {
    const changed = NAMES.some((name) => draft[name] !== undefined);
    body = (
      <form
        noValidate
        aria-labelledby={`${id}-title`}
        aria-busy={busy}
        onSubmit={save}
        className="border-border bg-surface flex flex-col gap-4 rounded-lg border p-4"
      >
        <div className="flex flex-col gap-1">
          <h2 id={`${id}-title`} className="font-medium">
            {t("caps")}
          </h2>
          <p className="text-sm">
            {data.monthly_usd !== null && data.daily_usd !== null
              ? t("current", { monthly: usd(data.monthly_usd), daily: usd(data.daily_usd) })
              : t("unset")}
          </p>
        </div>
        <div className="grid gap-4 sm:grid-cols-2">
          {NAMES.map((name) => (
            <TextField
              key={name}
              ref={(input) => {
                inputs.current[name] = input;
              }}
              label={t(`fields.${name}`)}
              name={name}
              inputMode="decimal"
              autoComplete="off"
              value={value(name)}
              // Mientras se guarda (también con el código abierto) no se escribe: al terminar,
              // el campo pasa a ser lo guardado y lo tecleado entretanto se perdería.
              readOnly={busy}
              onChange={(event) => {
                setDraft((before) => ({ ...before, [name]: event.target.value }));
                setFound((before) => {
                  const next = { ...before }; // lo dicho era de lo escrito antes
                  delete next[name];
                  if (next.daily_usd === "ABOVE") delete next.daily_usd; // compara los dos
                  return next;
                });
                setSaid(null);
              }}
              hint={t(`hints.${name}`)}
              error={found[name] ? t(`problems.${found[name]}`) : undefined}
            />
          ))}
        </div>
        <p className="bg-surface-raised rounded-lg p-3 text-sm">{t("note")}</p>
        <div className="flex flex-wrap gap-2">
          <Button
            type="submit"
            variant="primary"
            className="min-h-11 sm:min-h-9"
            aria-disabled={busy}
          >
            {busy ? t("busy") : t("save")}
          </Button>
          {changed && !busy ? (
            <Button
              type="button"
              variant="ghost"
              className="border-border min-h-11 border sm:min-h-9"
              onClick={() => {
                setDraft({});
                setFound({});
                setSaid(null);
                inputs.current.monthly_usd?.focus();
              }}
            >
              {t("discard")}
            </Button>
          ) : null}
        </div>
      </form>
    );
  } else if (query.isError && query.error.status !== 401) {
    // Sin el segundo factor reciente (también para leer), el código se pide al llegar y se puede
    // volver a abrir; cualquier otro error, con un reintento a mano.
    const step = query.error.code === STEP_UP_REQUIRED;
    body = (
      <div className="flex flex-col items-start gap-3">
        <p role="alert" className={step ? undefined : "text-danger"}>
          {step ? t("stepUp") : errors(`api.${apiErrorKey(query.error)}`)}
        </p>
        <Button
          variant="primary"
          size="lg"
          onClick={step ? () => setDismissed(false) : () => void query.refetch()}
        >
          {step ? ai("verify") : errors("retry")}
        </Button>
        {step && !dismissed && !query.isFetching ? (
          <StepUpDialog
            onVerified={() => {
              setDismissed(true);
              void query.refetch().finally(() => setDismissed(false));
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
    <section className="flex max-w-3xl flex-col gap-6">
      <div className="flex flex-col gap-2">
        <p className="text-[13px] tracking-wide uppercase">{ai("eyebrow")}</p>
        <h1 className="text-[28px] leading-tight font-bold tracking-[0.02em]">{t("title")}</h1>
        <p className="text-muted">{t("intro", { organization: organization.name })}</p>
      </div>
      {body}
      {/* Siempre montado: un lector de pantalla anuncia el resultado cuando cambia. */}
      <p role="status" className={said && !said.alert ? "text-sm" : "sr-only"}>
        {said && !said.alert ? said.text : ""}
      </p>
      {said?.alert ? (
        <p role="alert" className="text-danger text-sm">
          {said.text}
        </p>
      ) : null}
      {dialog}
    </section>
  );
}
