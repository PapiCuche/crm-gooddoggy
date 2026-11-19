"use client";

import { useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import { LINK } from "@/components/auth/auth-card";
import { LoginForm } from "@/components/auth/login-form";
import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import {
  authSession,
  useAuthLogout,
  useInvitationsAccept,
  useInvitationsPreview,
} from "@/lib/api/client";
import type { InvitationAcceptRequest, InvitationPreview } from "@/lib/api/model";
import type { ApiError } from "@/lib/http";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja la pantalla ocupada
// La forma del secreto que emite la API (`token_urlsafe(32)`, `apps.members.invitation_links`).
const SECRET = /^[A-Za-z0-9_-]{43}$/;
const ALERT =
  "border-danger/40 text-danger rounded-[10px] border px-3 py-2 text-sm transition-[opacity,transform] duration-200 ease-(--ease-out) starting:translate-y-1 starting:opacity-0";
// Sin red fallan enseguida; la caché no retiene secreto ni contraseña (como `PasswordResetChange`).
const WRITE = { mutation: { networkMode: "always", gcTime: 250 } } as const;

type Step =
  | { name: "opening" | "checking" | "absent" | "create" | "ready" | "other" }
  | { name: "login"; ended?: boolean } // `ended`: la sesión con la que se entró ya no existe
  | { name: "waiting"; error: ApiError | null } // `null`: la vista previa no contestó
  | { name: "invalid"; unsure: boolean } // `unsure`: un intento sin respuesta pudo aceptarla
  | { name: "done"; organization: string; created: boolean };
const FIELDS = { first: "first_name", last: "last_name", password: "password", confirm: "confirm" };
type Field = keyof typeof FIELDS;
type Problems = Partial<Record<Field | "form", string>>;
const SECRET_FIELD = { type: "password", autoComplete: "new-password", maxLength: 1024 } as const;

// Acepta una invitación (ADR-020 §4 y §5). El secreto llega en el fragmento de la URL, que el
// navegador no envía al servidor: se saca de la barra de direcciones nada más leerlo, se guarda
// solo en memoria y viaja solo en el cuerpo de las peticiones. Abrir la página pide la vista
// previa, que no gasta el enlace; aceptar es siempre una pulsación, también con sesión. Con una
// cuenta que ya existe se entra aquí mismo (ADR-020 §4): navegar perdería el enlace.
export function InvitationAccept() {
  const t = useTranslations();
  const queryClient = useQueryClient();
  const { mutate: ask, reset: forgetPreview } = useInvitationsPreview(WRITE);
  const { mutate: send, reset: forgetAccept } = useInvitationsAccept(WRITE);
  // Su 401 es «ya no había sesión»: como salir, y sin ir al login, que perdería el enlace.
  const logout = useAuthLogout({
    mutation: { networkMode: "always", meta: { ownSessionEnd: true } },
  });
  const [step, setStep] = useState<Step>({ name: "opening" });
  const [invite, setInvite] = useState<InvitationPreview | null>(null);
  const [sending, setSending] = useState(false);
  const [problems, setProblems] = useState<Problems>({});
  const secret = useRef<string | null>(null);
  const opened = useRef(false);
  const unsure = useRef(false);
  // Una aceptación cuya respuesta aún no se leyó. No el estado `sending`, que es el de la última
  // pantalla pintada: el plazo puede vencer con la respuesta ya leída y sin pintar.
  const inFlight = useRef(false);
  const aim = useRef(false); // el próximo aviso por campo lleva el foco al primero
  const heading = useRef<HTMLElement | null>(null); // lo que sustituye a la pantalla anterior
  const hold = (node: HTMLElement | null) => void (heading.current = node);
  const formRef = useRef<HTMLFormElement>(null);
  const firstField = useRef<HTMLInputElement>(null);
  const acceptButton = useRef<HTMLButtonElement>(null);
  const leaving = useRef(false); // un cierre de sesión en vuelo

  const check = useCallback(() => {
    const token = secret.current;
    if (!token) return;
    setStep({ name: "checking" });
    ask(
      { data: { token } },
      {
        onSuccess: (found) => {
          setInvite(found);
          setStep(found.needs_account ? { name: "create" } : { name: "login" });
        },
        onError: (error) => {
          if (error.code === "INVALID_INVITATION_LINK") {
            secret.current = null; // no vale: no se vuelve a presentar
            setStep({ name: "invalid", unsure: false });
          } else setStep({ name: "waiting", error });
        },
        onSettled: () => forgetPreview(),
      },
    );
  }, [ask, forgetPreview]);

  // Lee el enlace y lo quita de la barra de direcciones y de su entrada del historial.
  const open = useCallback(() => {
    const given = window.location.hash.slice(1);
    const clean = window.location.pathname + window.location.search;
    window.history.replaceState(window.history.state, "", clean);
    // El router de Next guarda su propia copia de la URL, con el fragmento: se entera por un
    // `replaceState` sin su marca en el estado, y no antes de que este efecto termine.
    queueMicrotask(() => window.history.replaceState(null, "", clean));
    forgetPreview(); // lo que quedara en vuelo con el enlace anterior ya no cambia esta pantalla
    forgetAccept();
    setInvite(null);
    setSending(false);
    setProblems({});
    unsure.current = false;
    inFlight.current = false;
    secret.current = SECRET.test(given) ? given : null;
    if (secret.current) check();
    // Un enlace con otra forma no se pregunta: la respuesta sería la de uno que no vale.
    else setStep(given ? { name: "invalid", unsure: false } : { name: "absent" });
  }, [check, forgetPreview, forgetAccept]);
  useEffect(() => {
    if (!opened.current) {
      opened.current = true;
      open();
    }
    // Otro enlace abierto en esta pestaña solo cambia el fragmento: la página no se recarga.
    const another = () => {
      if (window.location.hash) open();
    };
    window.addEventListener("hashchange", another);
    return () => window.removeEventListener("hashchange", another);
  }, [open]);

  const busy = sending || step.name === "checking";
  useEffect(() => {
    if (!busy) return;
    const timer = setTimeout(() => {
      if (!sending) {
        forgetPreview(); // se deja de esperar esa respuesta
        // Solo si aún se esperaba: una respuesta que llegó justo antes y aún no se pintó, manda.
        setStep((now) => (now.name === "checking" ? { name: "waiting", error: null } : now));
        return;
      }
      if (!inFlight.current) return; // su respuesta ya se leyó: manda ella
      inFlight.current = false;
      forgetAccept();
      unsure.current = true; // sin respuesta, esa aceptación pudo llegar
      setSending(false);
      setProblems({ form: t("auth.stalled") });
    }, PATIENCE);
    return () => clearTimeout(timer);
  }, [busy, sending, forgetPreview, forgetAccept, t]);

  // El foco va a lo que sustituye a la pantalla anterior.
  const shown = step.name;
  useEffect(() => {
    if (shown === "create") firstField.current?.focus();
    else if (shown === "ready") acceptButton.current?.focus();
    else if (shown !== "opening" && shown !== "login") heading.current?.focus(); // el login, el suyo
  }, [shown]);

  // Lo que no se acepta se dice junto a su campo, con el foco en el primero; no al corregir.
  useEffect(() => {
    if (!aim.current) return;
    aim.current = false;
    formRef.current?.querySelector<HTMLInputElement>("[aria-invalid]")?.focus();
  }, [problems]);
  // Al corregir un campo, su aviso ya no describe lo escrito; al cambiar la contraseña, tampoco
  // el de su repetición.
  const fix = (key: Field) => () => {
    const gone = key === "password" ? (["password", "confirm"] as const) : [key];
    if (gone.some((one) => problems[one])) {
      setProblems((now) => ({
        ...now,
        ...Object.fromEntries(gone.map((one) => [one, undefined])),
      }));
    }
  };

  // Con la sesión abierta (ahora o de antes), se acepta al pulsar. Si es de otra cuenta, se dice
  // antes de gastar un intento; sin correo, decide el 409. Los dos correos llegan en su forma
  // canónica (D-F2-3): la comparación sin mayúsculas es solo por si acaso.
  const invited = invite?.email ?? "";
  const entered = useCallback(
    (email: string) => {
      setProblems({});
      const other = email && email.trim().toLowerCase() !== invited.trim().toLowerCase();
      setStep({ name: other ? "other" : "ready" });
    },
    [invited],
  );

  // Un 409 con sesión: o la sesión es de otra cuenta o ya no existe. Se pregunta quién es.
  function whose() {
    const now = (next: Step) => setStep((was) => (was.name === "ready" ? next : was));
    authSession({ signal: AbortSignal.timeout(PATIENCE) })
      .then(() => now({ name: "other" }))
      .catch((error: ApiError) => {
        if (error.status !== 401) return now({ name: "other" });
        queryClient.clear(); // la sesión leída ya no vale: el login no debe darla por abierta
        now({ name: "login", ended: true });
      })
      .finally(() => setSending(false));
  }

  function rejection(error: ApiError | null) {
    if (!error) return t("auth.stalled");
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    const said = ["NETWORK_ERROR", "RATE_LIMITED"].includes(error.code);
    return t(`errors.api.${said ? error.code : "INTERNAL_ERROR"}`);
  }

  function create(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const first = String(form.get("first_name") ?? "").trim();
    const password = String(form.get("password") ?? ""); // tal cual: los espacios cuentan
    const confirm = String(form.get("confirm") ?? "");
    const found: Problems = {};
    if (!first) found.first = t("invitationAccept.firstNameRequired");
    if (!password) found.password = t("invitationAccept.passwordRequired");
    else if (password !== confirm) found.confirm = t("invitationAccept.mismatch");
    if (Object.keys(found).length) {
      aim.current = true;
      setProblems(found);
      return;
    }
    const last = String(form.get("last_name") ?? "").trim();
    accept({ first_name: first, last_name: last, password });
  }

  function accept(account: Omit<InvitationAcceptRequest, "token">) {
    const token = secret.current;
    if (sending || !token) return;
    const from = step.name;
    let asking = false; // `whose` termina el envío
    setSending(true);
    setProblems({});
    inFlight.current = true;
    send(
      { data: { token, ...account } },
      {
        onSuccess: (done) => {
          secret.current = null; // ya está usado
          const created = done.account_created;
          setStep({ name: "done", organization: done.organization_name, created });
        },
        onError: (error) => {
          if (error.code === "INVALID_INVITATION_LINK") {
            secret.current = null;
            setStep({ name: "invalid", unsure: unsure.current });
          } else if (error.code === "ACCOUNT_EXISTS") {
            // Sin cuenta al abrir, pero ya la tiene: se entra con ella.
            if (from === "create") setStep({ name: "login" });
            else whose();
            asking = from !== "create";
          } else if (error.code === "VALIDATION_ERROR") {
            // Por campo y nunca con el texto de la API, que no dice qué regla falló.
            const has = (key: string) => Object.hasOwn(error.fields ?? {}, key);
            const known = has("first_name") || has("last_name") || has("password");
            aim.current = true;
            setProblems({
              first: has("first_name") ? t("invitationAccept.firstNameRequired") : undefined,
              last: has("last_name") ? t("invitationAccept.lastNameRefused") : undefined,
              password: has("password") ? t("invitationAccept.passwordRefused") : undefined,
              form: known ? undefined : t("errors.api.INTERNAL_ERROR"),
            });
          } else {
            if (error.code === "NETWORK_ERROR") unsure.current = true; // pudo llegar igualmente
            setProblems({ form: rejection(error) });
          }
        },
        onSettled: () => {
          inFlight.current = false;
          if (!asking) setSending(false);
          forgetAccept();
        },
      },
    );
  }

  // Sale de la otra cuenta; el enlace sigue en memoria para entrar con la buena.
  function leave() {
    if (leaving.current) return; // también antes de que se pinte la primera pulsación
    leaving.current = true;
    const out = () => {
      queryClient.clear(); // lo leído con la otra sesión ya no vale
      setStep((now) => (now.name === "other" ? { name: "login" } : now)); // no otro enlace abierto
    };
    setProblems({});
    logout.mutate(undefined, {
      onSuccess: out,
      onError: (error) => (error.status === 401 ? out() : setProblems({ form: rejection(error) })),
      onSettled: () => (leaving.current = false),
    });
  }

  const input = (key: Field, more: Partial<Parameters<typeof TextField>[0]>) => (
    <TextField
      label={t(`invitationAccept.${key}`)}
      name={FIELDS[key]}
      error={problems[key]}
      onInput={fix(key)}
      {...more}
    />
  );
  const title = (key: string, values?: Record<string, string>) => (
    <h2 ref={hold} tabIndex={-1} className="text-lg font-semibold focus-visible:outline-none">
      {t(key, values)}
    </h2>
  );
  const enter = (where = "/login") => (
    <p className="text-sm">
      <Link href={where} className={LINK}>
        {t(where === "/login" ? "invitationAccept.enter" : "invitationAccept.open")}
      </Link>
    </p>
  );
  const alert = problems.form ? (
    <p role="alert" className={ALERT}>
      {problems.form}
    </p>
  ) : null;

  if (step.name === "opening" || step.name === "checking") {
    return (
      // Con el foco tras «Volver a intentar», que ya no está.
      <p ref={hold} tabIndex={-1} role="status" className="text-muted focus-visible:outline-none">
        {t(`invitationAccept.${step.name}`)}
      </p>
    );
  }
  if (step.name === "absent" || step.name === "invalid") {
    const absent = step.name === "absent";
    return (
      <div className="flex flex-col gap-3">
        {title(absent ? "invitationAccept.absentTitle" : "invitationAccept.invalidTitle")}
        <p>{t(absent ? "invitationAccept.absentBody" : "invitationAccept.invalidBody")}</p>
        {step.name === "invalid" && step.unsure ? (
          <>
            <p>{t("invitationAccept.invalidUnsure")}</p>
            {enter()}
          </>
        ) : null}
      </div>
    );
  }
  if (step.name === "waiting") {
    return (
      <div className="flex flex-col gap-3">
        <p ref={hold} tabIndex={-1} role="alert" className={ALERT}>
          {rejection(step.error)}
        </p>
        <Button type="button" variant="primary" size="lg" className="w-full" onClick={check}>
          {t("invitationAccept.retry")}
        </Button>
      </div>
    );
  }
  if (step.name === "done") {
    return (
      <div className="flex flex-col gap-3">
        {title("invitationAccept.doneTitle", { organization: step.organization })}
        <p>
          {step.created
            ? t("invitationAccept.doneCreated", { email: invite?.email ?? "" })
            : t("invitationAccept.doneJoined")}
        </p>
        {enter(step.created ? "/login" : "/o")}
      </div>
    );
  }

  const email = invite?.email ?? "";
  const intro = (
    <p className="text-muted">
      {t("invitationAccept.intro", { organization: invite?.organization_name ?? "", email })}
    </p>
  );
  if (step.name === "login") {
    return (
      <>
        {intro}
        {step.ended ? (
          <p role="alert" className={ALERT}>
            {t("errors.api.NOT_AUTHENTICATED")}
          </p>
        ) : null}
        <p>{t("invitationAccept.loginIntro")}</p>
        <LoginForm next="/o" onEntered={entered} />
        {/* En otra pestaña: esta guarda el enlace, que no sobrevive a una navegación. */}
        <p className="text-sm">
          <a href="/recuperar" target="_blank" rel="noopener noreferrer" className={LINK}>
            {t("auth.forgot")}
          </a>{" "}
          <span className="text-muted">{t("invitationAccept.forgotHint")}</span>
        </p>
      </>
    );
  }
  if (step.name === "ready" || step.name === "other") {
    const ready = step.name === "ready";
    return (
      <div className="flex flex-col gap-3">
        {intro}
        {ready ? (
          <p>{t("invitationAccept.readyIntro")}</p>
        ) : (
          <p ref={hold} tabIndex={-1} className="focus-visible:outline-none">
            {t("invitationAccept.otherAccount", { email })}
          </p>
        )}
        {alert}
        {/* `aria-disabled` y no `disabled`: conserva el foco mientras se envía. */}
        <Button
          ref={ready ? acceptButton : undefined}
          type="button"
          variant="primary"
          size="lg"
          aria-disabled={ready ? sending : logout.isPending}
          className="w-full"
          onClick={ready ? () => accept({}) : leave}
        >
          {ready
            ? t(sending ? "invitationAccept.accepting" : "invitationAccept.accept")
            : t(logout.isPending ? "invitationAccept.loggingOut" : "invitationAccept.logout")}
        </Button>
      </div>
    );
  }
  return (
    <>
      {intro}
      <p>{t("invitationAccept.createIntro")}</p>
      {/* `method`: un envío antes de que cargue el JavaScript nunca pone la contraseña en la URL. */}
      <form
        ref={formRef}
        noValidate
        method="post"
        onSubmit={create}
        className="flex flex-col gap-4"
        aria-busy={sending}
      >
        <TextField
          label={t("invitationAccept.email")}
          name="email"
          type="email"
          value={email}
          readOnly
          autoComplete="username"
        />
        {input("first", { ref: firstField, autoComplete: "given-name", maxLength: 150 })}
        {input("last", { autoComplete: "family-name", maxLength: 150 })}
        {input("password", { hint: t("invitationAccept.rules"), ...SECRET_FIELD })}
        {input("confirm", SECRET_FIELD)}
        {alert}
        <Button
          type="submit"
          variant="primary"
          size="lg"
          aria-disabled={sending}
          className="mt-1 w-full"
        >
          {sending ? t("invitationAccept.accepting") : t("invitationAccept.create")}
        </Button>
      </form>
    </>
  );
}
