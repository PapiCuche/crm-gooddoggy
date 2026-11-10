import type { ApiError } from "@/lib/http";

// Los errores propios de conectar o rotar, por `code` y nunca por el texto de la respuesta: el
// servidor no manda nada que se pinte, y un código desconocido cae en el mensaje genérico.
// Devuelve el texto (de `t`, que es el espacio `channels.errors`) y, si es de un campo, cuál.
export function channelError(
  error: ApiError,
  t: (key: string, values?: Record<string, number>) => string,
): { field?: "access_token"; text: string } | null {
  switch (error.code) {
    case "NOT_FOUND": // la cuenta ya no existe (otra persona la desconectó)
    case "CHANNEL_NOT_ENABLED":
    case "CHANNEL_PROVIDER_UNAVAILABLE":
    case "STEP_UP_REQUIRED":
      return { text: t(error.code) };
    case "CHANNEL_CREDENTIALS_REJECTED":
      return { field: "access_token", text: t(error.code) };
    case "RATE_LIMITED":
      // `Retry-After` en minutos, redondeado hacia arriba; sin él, el texto sin cifra.
      return {
        text: error.retryAfter
          ? t("RATE_LIMITED_WAIT", { minutes: Math.ceil(error.retryAfter / 60) })
          : t("RATE_LIMITED"),
      };
    default:
      return null;
  }
}
