import { createFileRoute } from "@tanstack/react-router"
import { ReactView } from "../legacy/views"
import { MemoryPage } from "../pages/memory"

export const Route = createFileRoute("/settings/memory")({ component: () => <ReactView view="memory"><MemoryPage /></ReactView> })
