import { startTransition, useEffect, useState } from "react";

// `useSearchParams` en jsdom: como en Next, la URL de `history` y `popstate` llega en una transición.
for (const name of ["pushState", "replaceState"] as const) {
  const original = history[name].bind(history);
  history[name] = (...args) => (original(...args), dispatchEvent(new PopStateEvent("popstate")));
}
export function useSearchParams() {
  const [search, setSearch] = useState(location.search);
  useEffect(() => {
    const sync = () => startTransition(() => setSearch(location.search));
    addEventListener("popstate", sync);
    return () => removeEventListener("popstate", sync);
  }, []);
  return new URLSearchParams(search);
}
