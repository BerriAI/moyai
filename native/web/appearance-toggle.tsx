import React, { useSyncExternalStore } from "react"
import { Moon, Sun } from "lucide-react"
import { Button } from "@/components/ui/button"
import { currentAppearance, setAppearance, subscribeAppearance } from "./appearance"
import "./appearance.css"

export function AppearanceToggle() {
  const appearance = useSyncExternalStore(subscribeAppearance, currentAppearance)
  const next = appearance === "dark" ? "light" : "dark"
  const label = `Switch to ${next} mode`
  return <Button type="button" variant="ghost" size="icon-sm" className="appearance-toggle"
    aria-label={label} title={label} onClick={() => setAppearance(next)}>
    {appearance === "dark" ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}
  </Button>
}
