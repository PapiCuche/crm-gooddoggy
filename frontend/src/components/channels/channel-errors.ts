import type { ApiError } from "@/lib/http";

// Los códigos que esta pantalla explica, tal como los manda el backend (`channel_errors` y
// `core.api.errors`); una prueba los busca en el contrato para que un cambio de nombre falle.
export const CHANNEL_CODES = [
  "NOT_FOUND", // la cuenta ya no existe (otra persona la desconectó)
  "CHANNEL_NOT_ENABLED",
  "CHANNEL_PROVIDER_UNAVAILABLE",
  "STEP_UP_REQUIRED",
  "CHANNEL_CREDENTIALS_REJECTED",
  "RATE_LIMITED",
] as const;
const RETRY_MAX = 3600; // `Retry-After` creíble: de 1 s a una hora; lo demás, sin cifra

// Los errores propios de conectar o rotar, por `code` y nunca por el texto de la respuesta: el
// servidor no manda nada que se pinte, y un código desconocido cae en el mensaje genérico.
// Devuelve el texto (de `t`, que es el espacio `channels.errors`) y, si es de un campo, cuál.
// `social`: Messenger o Instagram, con su propio texto donde WhatsApp nombra el número.
export function channelError(
  error: ApiError,
  t: (key: string, values?: Record<string, number>) => string,
  social = false,
): { field?: "access_token"; text: string } | null {
  const wait = error.retryAfter;
  switch (error.code) {
    case "NOT_FOUND":
    case "CHANNEL_PROVIDER_UNAVAILABLE":
    case "STEP_UP_REQUIRED":
      return { text: t(error.code) };
    case "CHANNEL_NOT_ENABLED":
      return { text: t(social ? "SOCIAL_NOT_ENABLED" : error.code) };
    case "CHANNEL_CREDENTIALS_REJECTED":
      return { field: "access_token", text: t(social ? "SOCIAL_REJECTED" : error.code) };
    case "RATE_LIMITED":
      // `Retry-After` en minutos, redondeado hacia arriba; sin él, o fuera de rango, sin cifra.
      return {
        text:
          Number.isInteger(wait) && wait! >= 1 && wait! <= RETRY_MAX
            ? t("RATE_LIMITED_WAIT", { minutes: Math.ceil(wait! / 60) })
            : t("RATE_LIMITED"),
      };
    default:
      return null;
  }
}
