import { queryOptions } from "@tanstack/react-query"
import { unwrap } from "./client"
import { getMemoryLibrary, getSessionTitleConfiguration, getWorkspaceSession } from "./generated/sdk.gen"

// Query key factory: each scope's `_def` is the prefix shared by all of its
// keys, for invalidating the whole scope. Per-user keys take the user ID so an
// identity change never shows the previous user's data.
export const queries = {
  session: {
    _def: ["session"] as const,
    current: (userId: string | null) => queryOptions({
      queryKey: ["session", "current", userId] as const,
      queryFn: async ({ signal }) => unwrap(await getWorkspaceSession({ signal })),
    }),
  },
  sessionTitles: {
    _def: ["session-titles"] as const,
    configuration: () => queryOptions({
      queryKey: ["session-titles", "configuration"] as const,
      queryFn: async ({ signal }) => unwrap(await getSessionTitleConfiguration({ signal })),
    }),
  },
  memory: {
    _def: ["memory"] as const,
    library: (userId: string | null) => queryOptions({
      queryKey: ["memory", "library", userId] as const,
      queryFn: async ({ signal }) => unwrap(await getMemoryLibrary({ signal })),
    }),
  },
}
