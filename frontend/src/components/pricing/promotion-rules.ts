import type { DiscountTypeEnum } from "@/lib/api/model";

import { cents, validAmount } from "./price-form";

// Los límites de la API (`apps.pricing.promotions`, `services.check_channel`).
export const [PRIORITY_MAX, CHANNELS_MAX, CHANNEL_MAX] = [10_000, 10, 30];
// Lo opcional del formulario. Vacío o sin tocar es lo que la API toma por omisión: no se envía.
export type Extra = { priority: string; channels: string[]; draft: string; ai: boolean };

// Un valor de descuento: mayor que 0 y, si es porcentaje, hasta 100. Decide la API; esto solo
// evita el viaje de lo evidente.
export const validValue = (type: DiscountTypeEnum, text: string) =>
  validAmount(text) && (type !== "PERCENT_OFF" || cents(text) <= 100_00);

// La prioridad: vacía (0) o un entero de -10000 a 10000.
export const validPriority = (text: string) =>
  text === "" || (/^-?\d{1,5}$/.test(text) && Math.abs(Number(text)) <= PRIORITY_MAX);

// Un canal nuevo: texto ya sin espacios exteriores, de hasta 30 caracteres, que no esté ya
// (distingue mayúsculas, como la API) y sin pasar de 10.
export const channelFits = (channels: string[], name: string) =>
  !!name &&
  name.length <= CHANNEL_MAX &&
  !channels.includes(name) &&
  channels.length < CHANNELS_MAX;
