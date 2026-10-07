import { ApiError } from "./http";

type Page<Row> = { results: Row[]; next?: string | null };

// Todas las páginas de un listado por cursor (ADR-016), en orden. Para lo que una pantalla
// necesita entero, como un directorio con el que emparejar.
export async function allPages<Row>(
  fetchPage: (cursor: string | undefined) => Promise<Page<Row>>,
): Promise<Row[]> {
  const found: Row[] = [];
  const seen = new Set<string>();
  let cursor: string | undefined;
  do {
    const page = await fetchPage(cursor);
    found.push(...page.results);
    cursor = page.next ?? undefined;
    // Un cursor repetido no avanza: es un fallo de la API, no una lista sin fin.
    if (cursor && seen.has(cursor)) throw new ApiError(500, "INTERNAL_ERROR");
    if (cursor) seen.add(cursor);
  } while (cursor);
  return found;
}
