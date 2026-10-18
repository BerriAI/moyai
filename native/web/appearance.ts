export type Appearance = "light" | "dark"

const storageKey = "moyai-appearance"
const changeEvent = "moyai:appearance-change"

function savedAppearance(): Appearance {
  try { return localStorage.getItem(storageKey) === "light" ? "light" : "dark" }
  catch { return "dark" }
}

export function currentAppearance(): Appearance {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark"
}

function applyAppearance(appearance: Appearance) {
  const root = document.documentElement
  root.dataset.theme = appearance
  root.classList.toggle("dark", appearance === "dark")
  root.style.colorScheme = appearance
  document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", appearance)
  const canvas = getComputedStyle(root).getPropertyValue("--ui-canvas").trim()
  document.querySelector('meta[name="theme-color"]')?.setAttribute("content", canvas || (appearance === "light" ? "#ffffff" : "#111111"))
}

export function initializeAppearance() {
  applyAppearance(savedAppearance())
}

export function setAppearance(appearance: Appearance) {
  applyAppearance(appearance)
  try { localStorage.setItem(storageKey, appearance) } catch { /* Keep the choice for this page when storage is unavailable. */ }
  window.dispatchEvent(new Event(changeEvent))
}

export function subscribeAppearance(onChange: () => void) {
  const onStorage = (event: StorageEvent) => {
    if (event.key !== storageKey && event.key !== null) return
    try { if (event.storageArea !== localStorage) return } catch { return }
    applyAppearance(event.newValue === "light" ? "light" : "dark")
    onChange()
  }
  window.addEventListener(changeEvent, onChange)
  window.addEventListener("storage", onStorage)
  return () => {
    window.removeEventListener(changeEvent, onChange)
    window.removeEventListener("storage", onStorage)
  }
}
