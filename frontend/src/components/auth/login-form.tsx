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

const PATIENCE = 30_000; // ms: un servidor que no contesta no deja el formulario ocupado

// Formulario de acceso. La sesión es una cookie HttpOnly que pone la API: aquí no se guarda
// nada. El rechazo no dice si falló el correo o la contraseña (la API tampoco lo distingue).
export function LoginForm({ next }: { next: string }) {
  const t = useTranslations();
  const router = useRouter();
  const queryClient = useQueryClient();
  const session = useAuthSession({ query: { retry: false, staleTime: 0 } });
  // `always`: sin red el intento falla enseguida; no queda en cola para enviarse más tarde.
  const login = useAuthLogin({ mutation: { networkMode: "always" } });
  const [missing, setMissing] = useState({ email: false, password: false });
  const [stalled, setStalled] = useState(false);
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
    answered.current = false;
    login.mutate(
      { data: { email, password } },
      {
        onSettled: () => (answered.current = true),
        onSuccess: () => {
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
      {login.isError || stalled ? (
        <p
          role="alert"
          className="border-danger/40 text-danger rounded-[10px] border px-3 py-2 text-sm transition-[opacity,transform] duration-200 ease-(--ease-out) starting:translate-y-1 starting:opacity-0"
        >
          {login.isError ? rejection(login.error) : t("auth.stalled")}
        </p>
      ) : null}
      <Button type="submit" variant="primary" size="lg" disabled={busy} className="mt-1 w-full">
        {busy ? t("auth.submitting") : t("auth.submit")}
        {busy ? null : <span aria-hidden>→</span>}
      </Button>
    </form>
  );
}
