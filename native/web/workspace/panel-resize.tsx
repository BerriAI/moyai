import { createContext, useCallback, useContext, useId, useLayoutEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from "react"
import { createPortal } from "react-dom"
import {
  DEFAULT_CHAT_RATIO, PanelResizeDrag, decodePanelRatio, panelKeyboardRatio, panelLayout,
  panelRatioStorageKey, readPanelRatio, validChatRatio, writePanelRatio, type PanelLayout, type RatioStorage,
} from "./panel-resize-state"

type Preference = { key: string | null; ratio: number; setRatio: (ratio: number) => void }
const PanelRatioContext = createContext<Preference | null>(null)
function browserStorage(): RatioStorage | undefined {
  try { return typeof window === "undefined" ? undefined : window.localStorage } catch { return undefined }
}
export function PanelRatioProvider({ scope, userId, children }: { scope: string; userId: string; children: ReactNode }) {
  const key = panelRatioStorageKey(scope, userId)
  const [saved, setSaved] = useState(() => ({ key, ratio: readPanelRatio(browserStorage(), key) }))
  const ratio = saved.key === key ? saved.ratio : DEFAULT_CHAT_RATIO
  useLayoutEffect(() => {
    setSaved(previous => previous.key === key ? previous : { key, ratio: readPanelRatio(browserStorage(), key) })
    const changed = (event: StorageEvent) => {
      if (event.storageArea && event.storageArea !== browserStorage()) return
      if (event.key !== key && event.key !== null) return
      const next = decodePanelRatio(event.newValue)
      setSaved(previous => previous.key === key && previous.ratio === next ? previous : { key, ratio: next })
    }
    window.addEventListener("storage", changed)
    return () => window.removeEventListener("storage", changed)
  }, [key])
  const setRatio = useCallback((next: number) => {
    if (!validChatRatio(next)) return
    setSaved(previous => previous.key === key && previous.ratio === next ? previous : { key, ratio: next })
    writePanelRatio(browserStorage(), key, next)
  }, [key])
  const value = useMemo(() => ({ key, ratio, setRatio }), [key, ratio, setRatio])
  return <PanelRatioContext.Provider value={value}>{children}</PanelRatioContext.Provider>
}
const percentage = (value: number) => Math.round(value * 1000) / 10

export function PanelResizeHandle({ workspace, enabled, chatId }: { workspace: RefObject<HTMLDivElement | null>; enabled: boolean; chatId: string }) {
  const preference = useContext(PanelRatioContext)
  if (!preference) throw new Error("Panel resizing requires its signed-in preference provider.")
  const [layout, setLayout] = useState<PanelLayout>(() => panelLayout(0, preference.ratio))
  const [dragging, setDragging] = useState(false)
  const separator = useRef<HTMLDivElement>(null), drag = useRef<PanelResizeDrag | null>(null)
  const removeListeners = useRef<(() => void) | null>(null), frame = useRef<number | null>(null)
  const draft = useRef<number | null>(null), helpId = useId()
  function apply(ratio: number) {
    const host = workspace.current
    if (!host) return
    const next = panelLayout(host.getBoundingClientRect().width, ratio, window.innerWidth)
    host.style.setProperty("--workspace-chat-width", `${next.chat}px`)
    host.classList.toggle("panel-split-compact", !next.split)
    setLayout(previous => previous.width === next.width && previous.chat === next.chat && previous.panel === next.panel && previous.split === next.split ? previous : next)
  }
  function stop(save: boolean, pointerId?: number) {
    const active = drag.current
    if (!active || (pointerId !== undefined && pointerId !== active.pointerId)) return
    drag.current = null
    const next = save ? active.finish(active.pointerId) : null
    active.cancel(); draft.current = null
    removeListeners.current?.(); removeListeners.current = null
    if (frame.current !== null) { cancelAnimationFrame(frame.current); frame.current = null }
    try { if (separator.current?.hasPointerCapture(active.pointerId)) separator.current.releasePointerCapture(active.pointerId) } catch {}
    workspace.current?.classList.remove("workspace-resizing")
    setDragging(false)
    if (next !== null) preference!.setRatio(next)
    apply(next ?? preference!.ratio)
  }
  useLayoutEffect(() => {
    const host = workspace.current
    if (!host) return
    const measure = () => {
      const width = host.getBoundingClientRect().width
      if (drag.current && (!enabled || !panelLayout(width, preference.ratio, window.innerWidth).split || Math.abs(width - drag.current.width) > 0.5)) stop(false)
      apply(draft.current ?? preference.ratio)
    }
    measure()
    const observer = new ResizeObserver(measure)
    observer.observe(host)
    window.addEventListener("resize", measure)
    return () => {
      observer.disconnect(); window.removeEventListener("resize", measure)
      stop(false)
    }
  }, [workspace, enabled, preference.ratio, preference.key])
  if (!enabled || !layout.split) return null
  return <>
    <div ref={separator} className={`panel-resize-handle${dragging ? " dragging" : ""}`} role="separator" tabIndex={0}
      aria-label="Resize chat and side panel" aria-orientation="vertical" aria-controls={chatId} aria-describedby={helpId}
      aria-valuemin={percentage(layout.minRatio)} aria-valuemax={percentage(layout.maxRatio)} aria-valuenow={percentage(layout.chat / layout.available)}
      aria-valuetext={`${percentage(layout.chat / layout.available)}% chat, ${percentage(layout.panel / layout.available)}% side panel`}
      title="Drag to resize. Arrow keys adjust; Shift uses larger steps. Enter resets 65:35."
      onMouseDown={event => { event.preventDefault(); event.stopPropagation() }}
      onPointerDown={event => {
        if (event.button !== 0 || !event.isPrimary || drag.current) return
        const host = workspace.current
        if (!host) return
        event.preventDefault(); event.stopPropagation(); event.currentTarget.focus({ preventScroll: true })
        const rect = host.getBoundingClientRect()
        drag.current = new PanelResizeDrag(event.pointerId, preference.ratio, rect.width, event.clientX, rect.left, window.innerWidth)
        try { event.currentTarget.setPointerCapture(event.pointerId) } catch {}
        host.classList.add("workspace-resizing"); setDragging(true)
        const move = (moveEvent: PointerEvent) => {
          if (moveEvent.pointerId !== drag.current?.pointerId) return
          moveEvent.preventDefault(); moveEvent.stopImmediatePropagation()
          const bounds = host.getBoundingClientRect()
          const ratio = drag.current.move(moveEvent.pointerId, moveEvent.clientX, bounds.left, bounds.width)
          if (ratio === null) { stop(false); return }
          draft.current = ratio
          if (frame.current === null) frame.current = requestAnimationFrame(() => {
            frame.current = null
            if (draft.current !== null) apply(draft.current)
          })
        }
        const end = (endEvent: PointerEvent) => {
          if (endEvent.pointerId !== drag.current?.pointerId) return
          endEvent.preventDefault(); endEvent.stopImmediatePropagation()
          const bounds = host.getBoundingClientRect()
          drag.current.move(endEvent.pointerId, endEvent.clientX, bounds.left, bounds.width)
          stop(true, endEvent.pointerId)
        }
        const cancel = () => stop(false)
        const escape = (keyEvent: KeyboardEvent) => {
          if (keyEvent.key === "Escape") { keyEvent.preventDefault(); keyEvent.stopImmediatePropagation(); stop(false) }
        }
        window.addEventListener("pointermove", move, { capture: true, passive: false })
        window.addEventListener("pointerup", end, { capture: true, passive: false })
        window.addEventListener("pointercancel", cancel, true)
        window.addEventListener("keydown", escape, true)
        window.addEventListener("blur", cancel)
        removeListeners.current = () => {
          window.removeEventListener("pointermove", move, true); window.removeEventListener("pointerup", end, true)
          window.removeEventListener("pointercancel", cancel, true); window.removeEventListener("keydown", escape, true)
          window.removeEventListener("blur", cancel)
        }
      }}
      onLostPointerCapture={() => stop(false)}
      onKeyDown={event => {
        if (event.ctrlKey || event.metaKey || event.altKey || event.nativeEvent.isComposing) return
        const host = workspace.current
        if (!host || !["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return
        event.preventDefault(); event.stopPropagation()
        stop(false)
        const next = panelKeyboardRatio(event.key, preference.ratio, host.getBoundingClientRect().width, window.innerWidth, event.shiftKey)
        if (next !== null) preference.setRatio(next)
      }}
    />
    <span id={helpId} className="visually-hidden">Use Left and Right arrows to resize. Shift changes by larger steps. Home and End set the limits. Enter restores 65 percent chat and 35 percent side panel. Escape cancels a drag.</span>
    {dragging && createPortal(<div className="panel-resize-shield" aria-hidden="true" onPointerDown={event => { event.preventDefault(); event.stopPropagation() }} />, document.body)}
  </>
}
