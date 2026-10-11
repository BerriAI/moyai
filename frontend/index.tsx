import { render, insert, replace, createElement, observeRemovals } from "./render"
import { createDialog, initializeShell } from "./overlays"
import "./theme.css"

export { render, insert, replace, createElement, createDialog }
export const isOpen = (host: HTMLElement) => host.dataset.open === "true"

initializeShell()
observeRemovals()
