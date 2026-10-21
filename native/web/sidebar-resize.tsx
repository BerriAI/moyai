import { useId, useLayoutEffect, useRef, useState } from "react"
import { createPortal } from "react-dom"
import "./sidebar-resize.css"

const MIN_WIDTH = 200
const defaultWidth = () => window.innerWidth <= 1250 ? 230 : 240
const maxWidth = () => Math.max(MIN_WIDTH, Math.min(440, window.innerWidth - 360))
const clamp = (width: number) => Math.round(Math.max(MIN_WIDTH, Math.min(maxWidth(), width)))

/** Ephemeral layout: the shell remounts this control when the conversation changes. */
export function SidebarResizeHandle({ enabled }: { enabled: boolean }) {
  const handle = useRef<HTMLDivElement>(null)
  const drag = useRef<{ id: number; x: number; width: number } | null>(null)
  const width = useRef(defaultWidth())
  const customized = useRef(false)
  const [layout, setLayout] = useState(() => ({ width: width.current, max: maxWidth() }))
  const [dragging, setDragging] = useState(false)
  const helpId = useId()

  function apply(next: number) {
    width.current = clamp(next)
    handle.current?.closest<HTMLElement>(".app")?.style.setProperty("--sidebar-width", `${width.current}px`)
    setLayout({ width: width.current, max: maxWidth() })
  }
  function stop(save: boolean) {
    const active = drag.current
    if (!active) return
    drag.current = null
    if (save) customized.current = true
    else apply(active.width)
    if (handle.current?.hasPointerCapture(active.id)) handle.current.releasePointerCapture(active.id)
    setDragging(false)
  }
  function reset() {
    stop(false)
    customized.current = false
    apply(defaultWidth())
  }
  useLayoutEffect(() => {
    const host = handle.current?.closest<HTMLElement>(".app")
    const resize = () => { stop(false); apply(customized.current ? width.current : defaultWidth()) }
    const cancel = () => stop(false)
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && drag.current) {
        event.preventDefault(); event.stopImmediatePropagation(); stop(false)
      }
    }
    resize()
    window.addEventListener("resize", resize)
    window.addEventListener("blur", cancel)
    window.addEventListener("keydown", escape, true)
    return () => {
      stop(false)
      host?.style.removeProperty("--sidebar-width")
      window.removeEventListener("resize", resize)
      window.removeEventListener("blur", cancel)
      window.removeEventListener("keydown", escape, true)
    }
  }, [])
  useLayoutEffect(() => { if (!enabled) stop(false) }, [enabled])

  return <>
    <div ref={handle} className={`sidebar-resize-handle${dragging ? " dragging" : ""}`} hidden={!enabled}
      role="separator" tabIndex={0} aria-label="Resize session sidebar" aria-orientation="vertical"
      aria-controls="session-sidebar" aria-describedby={helpId} aria-valuemin={MIN_WIDTH}
      aria-valuemax={layout.max} aria-valuenow={layout.width} aria-valuetext={`${layout.width} pixels`}
      title="Drag to resize. Arrow keys adjust. Double-click or Enter resets."
      onDoubleClick={reset}
      onPointerDown={event => {
        if (event.button !== 0 || !event.isPrimary || drag.current) return
        event.preventDefault(); event.stopPropagation()
        event.currentTarget.focus({ preventScroll: true })
        drag.current = { id: event.pointerId, x: event.clientX, width: width.current }
        event.currentTarget.setPointerCapture(event.pointerId)
        setDragging(true)
      }}
      onPointerMove={event => {
        const active = drag.current
        if (!active || active.id !== event.pointerId) return
        event.preventDefault(); event.stopPropagation()
        apply(active.width + event.clientX - active.x)
      }}
      onPointerUp={event => {
        const active = drag.current
        if (!active || active.id !== event.pointerId) return
        apply(active.width + event.clientX - active.x)
        stop(true)
      }}
      onPointerCancel={() => stop(false)} onLostPointerCapture={() => stop(false)}
      onKeyDown={event => {
        if (event.ctrlKey || event.metaKey || event.altKey || event.nativeEvent.isComposing) return
        if (!["ArrowLeft", "ArrowRight", "Home", "End", "Enter"].includes(event.key)) return
        event.preventDefault(); event.stopPropagation(); stop(false)
        if (event.key === "Enter") { reset(); return }
        const step = event.shiftKey ? 40 : 10
        customized.current = true
        apply(event.key === "Home" ? MIN_WIDTH : event.key === "End" ? maxWidth()
          : width.current + (event.key === "ArrowRight" ? step : -step))
      }} />
    <span id={helpId} className="visually-hidden">Use Left and Right arrows to resize, or Shift for larger steps. Home and End set the limits. Enter or double-click restores the default. Escape cancels a drag. Width resets in a new conversation.</span>
    {dragging && createPortal(<div className="sidebar-resize-shield" aria-hidden="true" />, document.body)}
  </>
}
