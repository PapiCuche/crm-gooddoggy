"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { type FormEvent, useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { TextField } from "@/components/ui/text-field";
import { useAuthLogin, useAuthSession } from "@/lib/api/client";
import { apiErrorKey } from "@/lib/api-errors";
import type { ApiError } from "@/lib/http";

import { LoginSecondStep } from "./login-second-step";

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado

// Formulario de acceso. La sesión es una cookie HttpOnly que pone la API: aquí no se guarda
// nada. El rechazo no dice si falló el correo o la contraseña (la API tampoco lo distingue).
// Si la cuenta tiene segundo factor, la contraseña correcta no abre sesión (202): la pantalla
// pasa al paso del código, en la misma tarjeta y sin navegar (ADR-023 §5).
export function LoginForm({ next }: { next: string }) {
  const t = useTranslations();
  const router = useRouter();
  const queryClient = useQueryClient();
  // `code`: la API espera el código. `expired`: lo esperaba y ya no; hay que empezar otra vez.
  const [second, setSecond] = useState<"no" | "code" | "expired">("no");
  // En el paso del código no se pregunta por la sesión al volver la ventana o la red: esa
  // petición lleva la cookie del acceso a medias y, si llega con el acceso ya completado, la
  // API borra la cookie de la sesión recién abierta (lo mismo que un segundo envío del código).
  const asks = second !== "code";
  const session = useAuthSession({
    query: { retry: false, staleTime: 0, refetchOnWindowFocus: asks, refetchOnReconnect: asks },
  });
  // `always`: sin red el intento falla enseguida; no queda en cola para enviarse más tarde.
  const login = useAuthLogin({ mutation: { networkMode: "always" } });
  const [missing, setMissing] = useState({ email: false, password: false });
  const [stalled, setStalled] = useState(false);
  const [email, setEmail] = useState(""); // el correo enviado: al volver del código sigue ahí
  const returned = useRef(false); // se acaba de volver del paso del código
  const emailField = useRef<HTMLInputElement>(null);
  const passwordField = useRef<HTMLInputElement>(null);
  const answered = useRef(false); // la respuesta del intento en curso ya se leyó

  useEffect(() => {
    if (session.isSuccess)
      router.replace(next); // ya tiene sesión: no vuelve a pedir la clave
    // Sin sesión, el foco va al correo; no antes (se iría a otra página) ni si ya escribe.
    else if (session.isError && document.activeElement === document.body) {
      emailField.current?.focus();
    }
  }, [session.isSuccess, session.isError, next, router]);

  useEffect(() => {
    if (second === "code" || !returned.current) return;
    returned.current = false;
    passwordField.current?.focus(); // el correo ya está: falta la contraseña, que no se guardó
  }, [second]);

  const { isPending, reset } = login;
  useEffect(() => {
    if (!isPending) return;
    const timer = setTimeout(() => {
      if (answered.current) return; // llegó justo antes y aún no se pintó: manda la respuesta
      reset(); // se deja de esperar esa respuesta: el formulario vuelve a estar disponible
      setStalled(true);
    }, PATIENCE);
    return () => clearTimeout(timer);
  }, [isPending, reset]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (login.isPending || login.isSuccess) return; // un segundo envío a la vez no cuenta
    const form = new FormData(event.currentTarget);
    const email = String(form.get("email") ?? "").trim();
    const password = String(form.get("password") ?? ""); // tal cual: los espacios cuentan
    setMissing({ email: !email, password: !password });
    if (!email || !password) {
      (email ? passwordField : emailField).current?.focus(); // al primer campo que falta
      return;
    }
    setStalled(false);
    setSecond("no");
    setEmail(email);
    answered.current = false;
    login.mutate(
      { data: { email, password } },
      {
        onSettled: () => (answered.current = true),
        onSuccess: (answer) => {
          // `Object`: un 2xx sin cuerpo llega como `null`, y `in` sobre `null` lanza.
          if ("mfa_required" in Object(answer)) {
            setSecond("code"); // falta el segundo paso: todavía no hay sesión
            return;
          }
          queryClient.clear(); // nada de lo que se leyó sin sesión sigue valiendo
          router.replace(next);
        },
      },
    );
  }

  // El límite de intentos dice cuánto esperar; lo demás, el mensaje de su `code`.
  function rejection(error: ApiError) {
    if (error.code === "RATE_LIMITED" && error.retryAfter) {
      return t("auth.rateLimited", { minutes: Math.ceil(error.retryAfter / 60) });
    }
    return t(`errors.api.${apiErrorKey(error)}`);
  }

  if (second === "code") {
    return (
      <LoginSecondStep
        next={next}
        onRestart={(expired) => {
          returned.current = true;
          setSecond(expired ? "expired" : "no");
          reset(); // el formulario de la contraseña vuelve a estar disponible
          // Una respuesta que se dejó de esperar pudo abrir la sesión: si la hay, entra.
          void session.refetch();
        }}
      />
    );
  }

  // Hasta que la navegación sustituye la página, también la de quien ya tenía sesión.
  const busy = login.isPending || login.isSuccess || session.isSuccess;
  return (
    // `method`: un envío antes de que cargue el JavaScript nunca pone la contraseña en la URL.
    <form
      noValidate
      method="post"
      onSubmit={submit}
      className="flex flex-col gap-4"
      aria-busy={busy}
    >
      <TextField
        ref={emailField}
        label={t("auth.email")}
        name="email"
        type="email"
        autoComplete="username"
        defaultValue={email}
        autoCapitalize="none"
        spellCheck={false}
        maxLength={254}
        error={missing.email ? t("auth.emailRequired") : undefined}
        onInput={() => missing.email && setMissing({ ...missing, email: false })}
      />
      <TextField
        ref={passwordField}
        label={t("auth.password")}
        name="password"
        type="password"
        autoComplete="current-password"
        maxLength={1024}
        error={missing.password ? t("auth.passwordRequired") : undefined}
        onInput={() => missing.password && setMissing({ ...missing, password: false })}
      />
      {login.isError || stalled || second === "expired" ? (
        <p
          role="alert"
          className="border-danger/40 text-danger rounded-[10px] border px-3 py-2 text-sm transition-[opacity,transform] duration-200 ease-(--ease-out) starting:translate-y-1 starting:opacity-0"
        >
          {login.isError
            ? rejection(login.error)
            : stalled
              ? t("auth.stalled")
              : t("auth.secondStepExpired")}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" disabled={busy} className="mt-1 w-full">
        {busy ? t("auth.submitting") : t("auth.submit")}
        {busy ? null : <span aria-hidden>→</span>}
      </Button>
    </form>
  );
}
