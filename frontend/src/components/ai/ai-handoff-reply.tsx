"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useFormatter, useTranslations } from "next-intl";
import { type FormEvent, type ReactNode, useId, useRef, useState } from "react";

import { useTenant } from "@/components/app-shell/tenant-context";
import { StepUpDialog } from "@/components/security/step-up-dialog";
import { isStepUpCancelled, STEP_UP_REQUIRED, useStepUp } from "@/components/security/use-step-up";
import { Button } from "@/components/ui/button";
import {
  aiHandoffReplyRetrieve,
  aiHandoffReplyUpdate,
  getAiHandoffReplyRetrieveQueryKey,
} from "@/lib/api/client";
import type { AiHandoffReply } from "@/lib/api/model";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";
import { cn } from "@/lib/utils";

import { aiError, toLogin } from "./ai-key-actions";
import { MANAGE } from "./ai-switch";

export const HANDOFF_MAX = 500; // el `HANDOFF_MAX` del backend
// Los códigos de `text` en el 400 de la API (`detectors.reply_problems`), más el vacío.
const PROBLEMS = new Set([
  ...["EMPTY", "BODY", "TOO_LONG", "SCRIPT", "SOON", "FIGURE", "DELIVERY", "DISCOUNT"],
  ...["AVAILABILITY", "TERMS", "LINK", "MARKUP"],
]);
const AREA =
  "bg-surface border-foreground/50 focus-visible:border-foreground aria-invalid:border-danger min-h-32 resize-y rounded-[10px] border px-3 py-2.5 text-[16px] leading-relaxed";
// Como `len` de Python: por punto de código, no por unidad UTF-16 (un emoji cuenta uno).
const count = (text: string) => Array.from(text).length;
type Said = { text: string; alert?: boolean } | null;

// Los problemas del texto en un 400, solo por su código: el texto del servidor nunca se pinta.
// Un código que esta versión no conoce se dice como `OTHER`; `null`: no es un error del texto.
function problems(error: ApiError): string[] | null {
  const found = error.status === 400 ? error.fields?.text : undefined;
  if (!Array.isArray(found)) return null;
  const codes = found.map((item: { code?: unknown } | null) => {
    const code = item?.code === "blank" ? "EMPTY" : item?.code;
    return typeof code === "string" && PROBLEMS.has(code) ? code : "OTHER";
  });
  return [...new Set(codes)];
}

// El texto de traspaso (F8-09c, #1057): lo que el copiloto propone cuando el cliente pide una
// persona (F8-08l, #1046). Leerlo y cambiarlo piden `ai.manage` y el segundo factor reciente;
// guardar cuenta en el tope por persona de las escrituras (429). No hay «volver al inicial»: la
// API no lo ofrece. Se valida aquí lo evidente (vacío, largo) y lo demás lo decide la API.
export function AiHandoffReplyPanel() {
  const t = useTranslations("ai.handoff");
  const ai = useTranslations("ai");
  const own = useTranslations("ai.errors");
  const errors = useTranslations("errors");
  const format = useFormatter();
  const { organization, permissions } = useTenant();
  const slug = organization.slug;
  const allowed = permissions.some((grant) => grant.code === MANAGE);
  const queryClient = useQueryClient();
  const key = getAiHandoffReplyRetrieveQueryKey(slug);
  const query = useQuery<AiHandoffReply, ApiError>({
    queryKey: key,
    queryFn: ({ signal }) => aiHandoffReplyRetrieve(slug, { signal }),
    enabled: allowed, // sin el permiso no se pide: sería un 403
    ...{ staleTime: Infinity, refetchOnWindowFocus: false, refetchOnReconnect: false },
    retry: false,
  });
  const { withStepUp, dialog } = useStepUp();
  const [draft, setDraft] = useState<string | null>(null); // null: el guardado, sin tocar
  const [found, setFound] = useState<string[]>([]);
  const [said, setSaid] = useState<Said>(null);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false); // el código de la lectura, cancelado
  const sending = useRef(false); // dos pulsaciones seguidas no son dos escrituras
  const area = useRef<HTMLTextAreaElement>(null);
  const id = useId();

  const data = query.data;
  const value = draft ?? data?.text ?? "";
  const length = count(value.trim()); // la API recorta los blancos de los bordes
  const fail = (codes: string[]) => {
    setFound(codes);
    area.current?.focus(); // un solo campo: el foco va a él, descrito por sus problemas
  };

  async function save(event: FormEvent) {
    event.preventDefault();
    if (sending.current || !data) return;
    const text = value.trim();
    setSaid(null);
    if (!text) return fail(["EMPTY"]);
    if (count(text) > HANDOFF_MAX) return fail(["TOO_LONG"]);
    setFound([]);
    if (text === data.text && !data.initial) return setSaid({ text: t("unchanged") });
    sending.current = true;
    setBusy(true);
    try {
      const saved = await withStepUp(() => aiHandoffReplyUpdate(slug, { text }));
      queryClient.setQueryData(key, saved);
      setDraft(null);
      setSaid({ text: t("saved") });
    } catch (caught) {
      const error = caught as ApiError;
      const cancelled: boolean = isStepUpCancelled(error); // cancelar el código no es un fallo
      if (cancelled || toLogin(error)) return;
      const codes = problems(error);
      if (codes) return fail(codes);
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
    const over = length - HANDOFF_MAX;
    const describedBy = [`${id}-note`, `${id}-count`, found.length ? `${id}-problems` : null];
    body = (
      <form
        noValidate
        aria-labelledby={`${id}-label`}
        onSubmit={save}
        className="border-border bg-surface flex flex-col gap-3 rounded-lg border p-4"
      >
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <label id={`${id}-label`} htmlFor={`${id}-text`} className="font-medium">
            {t("label")}
          </label>
          <p className="text-sm font-medium">{t(data.initial ? "initial" : "custom")}</p>
        </div>
        <p className="text-muted text-sm">
          {t(data.initial ? (data.changed_at ? "invalidNote" : "initialNote") : "customNote")}
          {data.changed_at ? (
            <>
              {" "}
              {t("changed")}{" "}
              <time dateTime={data.changed_at}>
                {format.dateTime(new Date(data.changed_at), {
                  ...{ day: "numeric", month: "short", year: "numeric" },
                  ...{ hour: "2-digit", minute: "2-digit" },
                })}
              </time>
            </>
          ) : null}
        </p>
        <textarea
          ref={area}
          id={`${id}-text`}
          rows={5}
          value={value}
          // Mientras se guarda (también con el código abierto) no se escribe: al terminar, el
          // campo pasa a ser lo guardado y lo tecleado entretanto se perdería.
          readOnly={busy}
          onChange={(event) => {
            setDraft(event.target.value);
            setFound([]); // lo dicho era del texto anterior
            setSaid(null);
          }}
          aria-invalid={found.length ? true : undefined}
          aria-describedby={describedBy.filter(Boolean).join(" ")}
          className={AREA}
        />
        <p
          id={`${id}-count`}
          className={cn("text-sm tabular-nums", over > 0 ? "text-danger" : "text-muted")}
        >
          {over > 0
            ? t("over", { count: length, max: HANDOFF_MAX, over })
            : t("count", { count: length, max: HANDOFF_MAX })}
        </p>
        <p id={`${id}-note`} className="bg-surface-raised rounded-lg p-3 text-sm">
          {t("note")}
        </p>
        {found.length ? (
          <div id={`${id}-problems`} role="alert" className="text-danger text-sm">
            <p>{t("problemsTitle")}</p>
            <ul className="ml-5 list-disc">
              {found.map((code) => (
                <li key={code}>
                  {t(`problems.${code}` as "problems.TOO_LONG", { max: HANDOFF_MAX })}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="flex flex-wrap gap-2">
          <Button
            type="submit"
            variant="primary"
            className="min-h-11 sm:min-h-9"
            aria-disabled={busy}
          >
            {busy ? t("busy") : t("save")}
          </Button>
          {draft !== null && draft !== data.text ? (
            <Button
              type="button"
              variant="ghost"
              className="border-border min-h-11 border sm:min-h-9"
              onClick={() => {
                setDraft(null);
                setFound([]);
                setSaid(null);
                area.current?.focus();
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
