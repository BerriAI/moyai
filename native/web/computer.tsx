import React, { useCallback, useEffect, useRef, useState } from "react"
import { Camera, Globe, Maximize2, Monitor, MousePointer2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { api } from "./api"
import { DesktopInputQueue } from "./desktop-input"

type DesktopState = { available: boolean; has_workspace: boolean; controlled: boolean; busy: boolean; frame?: string; width?: number; height?: number }

export function ComputerPanel({ id, hasWorkspace, expanded, onExpand }: { id: string; hasWorkspace: boolean; expanded: boolean; onExpand: () => void }) {
  const [view, setView] = useState<DesktopState>({ available: false, has_workspace: hasWorkspace, controlled: false, busy: false })
  const [error, setError] = useState("")
  const [working, setWorking] = useState(false)
  const [halted, setHalted] = useState(false)
  const [url, setUrl] = useState("")
  const stage = useRef<HTMLDivElement>(null)
  const keyboard = useRef<HTMLTextAreaElement>(null)
  const image = useRef<HTMLImageElement>(null)
  const queue = useRef<DesktopInputQueue | null>(null)
  const stopped = useRef(false)
  const controlling = useRef(false)
  const ownsControl = useRef(false)
  ownsControl.current = view.controlled && !halted
  controlling.current = ownsControl.current && !working
  if (!queue.current) queue.current = new DesktopInputQueue(
    body => api(`/sessions/${id}/computer`, "POST", body),
    () => ownsControl.current && !stopped.current,
    error => {
      stopped.current = true; setHalted(true)
      setError(`${String(error)} Check the screen, then resume control. Input was not retried.`)
    },
  )
  const refresh = useCallback(async (signal?: AbortSignal) => {
    const next = await api<DesktopState>(`/sessions/${id}/computer`, "GET", undefined, signal)
    if (!signal?.aborted) setView(previous => ({ ...previous, ...next, frame: next.frame ?? previous.frame }))
  }, [id])
  useEffect(() => { stopped.current = false; return () => { stopped.current = true } }, [])
  useEffect(() => {
    const element = stage.current
    if (!element) return
    const wheel = (event: WheelEvent) => {
      if (!controlling.current || stopped.current) return
      event.preventDefault()
      const pixels = event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? element.clientHeight : 1)
      queue.current?.wheel(pixels)
    }
    element.addEventListener("wheel", wheel, { passive: false })
    return () => element.removeEventListener("wheel", wheel)
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { if (!working) await refresh(controller.signal) }
      catch (error) { if (!controller.signal.aborted) setError(String(error)) }
      if (!controller.signal.aborted) timer = setTimeout(poll, 1200)
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [refresh, working])
  useEffect(() => {
    if (!view.controlled || working) return
    const timer = setInterval(() => {
      void api(`/sessions/${id}/computer`, "POST", { action: "heartbeat" }).catch(error => {
        stopped.current = true; setHalted(true); setError(String(error))
      })
    }, 30000)
    return () => clearInterval(timer)
  }, [id, view.controlled, working])

  async function command(action: string, values: object = {}) {
    setWorking(true); setError("")
    if (action === "release") stopped.current = true
    try {
      await queue.current?.drain()
      const next = await api<Partial<DesktopState>>(`/sessions/${id}/computer`, "POST", { action, ...values })
      setView(previous => ({ ...previous, ...next }))
      if (action === "claim" || action === "start") { stopped.current = false; setHalted(false); keyboard.current?.focus({ preventScroll: true }) }
      await refresh()
    } catch (error) { setError(String(error)) } finally { setWorking(false) }
  }

  function input(action: string, values: Record<string, string | number | boolean>) {
    if (controlling.current) queue.current?.enqueue(action, values)
  }

  function key(event: React.KeyboardEvent) {
    if (!controlling.current || event.nativeEvent.isComposing) return
    if (event.ctrlKey && event.shiftKey && event.key === "Escape") {
      event.preventDefault(); void command("release"); return
    }
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") return
    if (["Shift", "Control", "Alt", "Meta", "CapsLock", "Dead", "Process", "Unidentified"].includes(event.key)) return
    event.preventDefault()
    if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) input("type", { text: event.key })
    else input("key", { key: event.key, ctrl: event.ctrlKey || event.metaKey, alt: event.altKey, shift: event.shiftKey })
  }

  return <div className={`computer-panel ${expanded ? "expanded" : ""}`}>
    <div className="computer-toolbar"><span><strong>Computer</strong><small>{working ? "Preparing desktop…" : view.controlled ? "You have control" : view.available ? "Live workspace" : hasWorkspace ? "Ready to open" : "Starts with your first task"}</small></span>
      <div><Button variant="outline" size="sm" disabled={working || (!view.available && !view.controlled) || view.busy} onClick={() => void command(view.controlled && !halted ? "release" : "claim")}><MousePointer2 size={14} />{halted ? "Resume control" : view.controlled ? "Release control" : "Take control"}</Button>
        <Button variant="ghost" size="icon-sm" aria-label={expanded ? "Restore panel size" : "Expand computer"} onClick={onExpand}><Maximize2 size={16} /></Button></div>
    </div>
    {error && <p className="error" role="alert">{error}</p>}
    <div className={`desktop-stage ${controlling.current ? "controlled" : ""}`} ref={stage} tabIndex={view.controlled ? 0 : -1} role="application" aria-label="Sandbox desktop. Take control to click and type."
      onFocus={event => { if (event.target === stage.current && controlling.current) keyboard.current?.focus({ preventScroll: true }) }}
      onKeyDown={key} onCompositionEnd={event => input("type", { text: event.data })}
      onPaste={event => {
        if (!controlling.current) return
        event.preventDefault()
        const text = event.clipboardData.getData("text/plain")
        if (text.length > 2000) { setError("Paste up to 2,000 characters at a time into the desktop."); return }
        input("type", { text })
      }}
      onClick={event => {
        if (!image.current || !controlling.current) return
        const bounds = image.current.getBoundingClientRect()
        const x = Math.floor((event.clientX - bounds.left) * (view.width ?? 1280) / bounds.width)
        const y = Math.floor((event.clientY - bounds.top) * (view.height ?? 800) / bounds.height)
        if (x >= 0 && y >= 0 && x < (view.width ?? 1280) && y < (view.height ?? 800)) {
          keyboard.current?.focus({ preventScroll: true }); input("click", { x, y })
        }
      }}>
      <textarea ref={keyboard} className="desktop-keyboard" aria-label="Desktop keyboard" tabIndex={-1} readOnly={!view.controlled} autoComplete="off" autoCapitalize="off" spellCheck={false} />
      {view.available && view.frame ? <img ref={image} src={`data:image/jpeg;base64,${view.frame}`} alt="Live Modal desktop" draggable={false} />
        : <div className="desktop-empty"><Monitor size={40} /><h3>Your computer in the cloud</h3><p>{working ? "Preparing your desktop. Your existing files stay in this workspace." : hasWorkspace ? "Open the desktop in the same computer Moyai uses for your work." : "Send Moyai a task. Its computer will appear here, ready for you to open and control."}</p>
          {hasWorkspace && <Button disabled={working || view.busy} onClick={() => void command("start")}>{working ? "Preparing desktop…" : "Open computer"}</Button>}</div>}
    </div>
    {view.busy && <p className="computer-hint">Moyai is working. Stop the task before taking control.</p>}
    {view.controlled && <p className="computer-hint">Click the desktop to type. Ctrl+Shift+Escape releases control. New agent tasks wait while you have control.</p>}
    <form className="browser-address" onSubmit={event => { event.preventDefault(); void command("open", { url }) }}>
      <Globe size={16} /><Input aria-label="Computer browser address" placeholder="https://example.com" value={url} onChange={event => setUrl(event.target.value)} disabled={!view.controlled || working} />
      <Button type="submit" variant="outline" size="sm" disabled={!view.controlled || working}>Open browser</Button>
    </form>
    <div className="computer-footer"><Button variant="outline" size="sm" disabled={!view.frame} onClick={() => { if (!view.frame) return; const link = document.createElement("a"); link.href = `data:image/jpeg;base64,${view.frame}`; link.download = "moyai-computer.jpg"; link.click() }}><Camera size={15} />Screenshot</Button><span>Watch Moyai work here. Take control to use the desktop.</span></div>
  </div>
}
