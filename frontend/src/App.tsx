import { QueryClient } from "@tanstack/react-query";
import type { RouteObject } from "react-router";

import { shouldRetry } from "./api/errors";
import { Home } from "./routes/Home";
import { NotFound } from "./routes/NotFound";
import { World } from "./routes/World";
import { Shell } from "./ui/Shell";

export const routes: RouteObject[] = [
  {
    element: <Shell />,
    children: [
      { index: true, element: <Home /> },
      { path: "world/:snapshotId", element: <World /> },
      { path: "*", element: <NotFound /> },
    ],
  },
];

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: shouldRetry, refetchOnWindowFocus: false },
    },
  });
}
