import { createFileRoute } from "@tanstack/react-router"
import { ControllerView } from "../legacy/views"

export const Route = createFileRoute("/sessions/new")({ component: () => <ControllerView view="tasks" /> })
