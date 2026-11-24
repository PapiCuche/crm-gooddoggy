import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach, vi } from "vitest";

vi.mock("next/navigation", async (original) => ({
  ...(await original<typeof import("next/navigation")>()),
  useSearchParams: (await import("./src/test-search-params")).useSearchParams,
}));

afterEach(() => (cleanup(), globalThis.history?.replaceState(null, "", "/"))); // sin `globals`, ni DOM ni URL pasan de un test a otro
