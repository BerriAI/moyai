import { createFileRoute } from "@tanstack/react-router"
import { ReactView } from "../legacy/views"
import { SettingsPage } from "../pages/settings"

export const Route = createFileRoute("/settings/")({ component: () => <ReactView view="settings"><SettingsPage /></ReactView> })
