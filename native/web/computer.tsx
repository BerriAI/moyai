import { appPath } from "./api"
import React, { useCallback, useEffect, useRef, useState } from "react"
import RFB from "@novnc/novnc"
import { Monitor, MousePointer2, RefreshCw, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { controlComputer, getComputer, type ComputerState } from "./generated/api"
import { DesktopLifetime, HeldKey, pasteRemote, pauseRemote, releaseRemoteKeys } from "./desktop-session"

type DesktopState = ComputerState
const CONTROL = 0xffe3
type PanelProps = { id: string; hasWorkspace: boolean; active?: boolean }

export function ComputerPanel(props: PanelProps) {
  // A different conversation gets an independent browser identity and cleanup.
  return <ComputerSession key={props.id} {...props} />
}

function ComputerSession({ id, hasWorkspace, active = true }: PanelProps) {
  const [view, setView] = useState<DesktopState>({ available: false, has_workspace: hasWorkspace, controlled: false, can_release: false, busy: false, controller: null })
  const [error, setError] = useState("")
  const [working, setWorking] = useState(false)
  const [connected, setConnected] = useState(false)
  const [connecting, setConnecting] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [visible, setVisible] = useState(!document.hidden)
  const foreground = active && visible
  const lifetime = useRef(new DesktopLifetime(foreground))
  lifetime.current.setVisible(foreground)
  const host = useRef<HTMLDivElement>(null)
  const pasteReceiver = useRef<HTMLTextAreaElement>(null)
  const remote = useRef<RFB | null>(null)
  const clientId = useRef(crypto.randomUUID())
  const changingControl = useRef(false)
  const heldModifiers = useRef(new Set<string>())
  const heldKeys = useRef(new Map<string, HeldKey>())
  const pointer = useRef({ buttons: new Set<number>(), x: 0, y: 0 })
  const mounted = useRef(true)
  const owned = useRef(false)
  const commandPending = useRef(false)
  const pauseInput = useCallback(() => {
    if (remote.current) {
      // Release pointer buttons through noVNC's existing event path before
      // disabling input. Disconnecting mid-drag must not leave a button down.
      const canvas = host.current?.querySelector("canvas")
      const { buttons, x: clientX, y: clientY } = pointer.current
      for (const button of buttons) canvas?.dispatchEvent(new MouseEvent("mouseup", { button, clientX, clientY, bubbles: true }))
      buttons.clear()
      pauseRemote(remote.current, heldKeys.current, key => {
        canvas?.dispatchEvent(new KeyboardEvent("keyup", { ...key, bubbles: true }))
      })
    }
    heldModifiers.current.clear()
  }, [])
  const releaseOwned = useCallback(() => {
    if (!owned.current) return
    owned.current = false
    void controlComputer({
      path: { session_id: id }, body: { action: "release", client_id: clientId.current }, keepalive: true,
    }).catch(() => {}) // A lost request retains the bounded lease until expiry.
  }, [id])
  const refresh = useCallback(async (signal?: AbortSignal) => {
    if (!lifetime.current.visible || commandPending.current) return
    const version = lifetime.current.invalidate()
    const { data: next } = await getComputer({ path: { session_id: id }, query: { client_id: clientId.current }, signal })
    if (!signal?.aborted && lifetime.current.accepts(version) && !commandPending.current) {
      owned.current = next.controlled
      setView(next)
    }
  }, [id])
  useEffect(() => {
    mounted.current = true
    lifetime.current.alive = true
    const leavePage = () => { pauseInput(); releaseOwned() }
    window.addEventListener("pagehide", leavePage)
    return () => {
      window.removeEventListener("pagehide", leavePage)
      mounted.current = false; lifetime.current.dispose(); pauseInput()
      releaseOwned()
    }
  }, [pauseInput, releaseOwned])

  useEffect(() => {
    const change = () => {
      if (document.hidden) { lifetime.current.setVisible(false); pauseInput() }
      setVisible(!document.hidden)
    }
    document.addEventListener("visibilitychange", change)
    return () => document.removeEventListener("visibilitychange", change)
  }, [pauseInput])

  useEffect(() => {
    if (!foreground) {
      pauseInput(); setConnected(false); setConnecting(false)
    }
  }, [foreground, pauseInput])

  useEffect(() => {
    if (!foreground) return
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { await refresh(controller.signal) }
      catch (error) {
        if (!controller.signal.aborted && !commandPending.current) {
          pauseInput()
          setError(String(error))
          setView(previous => ({ ...previous, available: false, controlled: false }))
        }
      }
      if (!controller.signal.aborted) timer = setTimeout(poll, 5000)
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [refresh, foreground, pauseInput])

  useEffect(() => {
    const element = host.current
    if (!foreground || !view.available || !element) return
    let live = true
    setConnected(false); setConnecting(true); setError("")
    const query = new URLSearchParams({ client_id: clientId.current, control: String(view.controlled) })
    const socket = new WebSocket(`${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}${appPath(`/api/sessions/${id}/computer/stream`)}?${query}`)
    const rfb = new RFB(element, socket, { shared: true })
    remote.current = rfb
    rfb.scaleViewport = true
    rfb.resizeSession = false
    rfb.background = "transparent"
    rfb.viewOnly = !view.controlled || changingControl.current || commandPending.current
    const open = () => {
      if (!live || !lifetime.current.visible) return
      setConnected(true); setConnecting(false); setError("")
      if (view.controlled && !commandPending.current) rfb.focus({ preventScroll: true })
    }
    const close = (event: CloseEvent) => {
      if (!live) return
      setConnected(false); setConnecting(false)
      element.replaceChildren()
      if (!changingControl.current) setError(event.reason || "The computer disconnected. Reconnect to continue.")
      // Reflect released control without replaying input or automatically
      // retrying a failed control connection.
      // Keep ownership known for explicit cleanup, but require Take control
      // or a fresh metadata check before reconnecting a controller.
      void refresh().catch(() => {})
    }
    rfb.addEventListener("connect", open)
    socket.addEventListener("close", close)
    return () => {
      live = false
      rfb.removeEventListener("connect", open)
      socket.removeEventListener("close", close)
      if (remote.current === rfb) pauseInput()
      if (socket.readyState !== WebSocket.CLOSED) rfb.disconnect()
      if (remote.current === rfb) remote.current = null
      element.replaceChildren()
    }
  }, [id, view.available, view.controlled, attempt, refresh, foreground, pauseInput])

  useEffect(() => {
    if (remote.current) remote.current.viewOnly = !foreground || !view.controlled || working
  }, [view.controlled, working, foreground])

  useEffect(() => {
    // Deliberately independent of foreground: switching panels does not hand
    // browser writes back to the agent. Unmount and explicit release do.
    if (!view.controlled) return
    const timer = setInterval(() => {
      void controlComputer({ path: { session_id: id }, body: { action: "heartbeat", client_id: clientId.current } }).catch(error => {
        if (!mounted.current) return
        pauseInput(); owned.current = false
        setView(previous => ({ ...previous, controlled: false, available: false }))
        setError(String(error))
      })
    }, 30000)
    return () => clearInterval(timer)
  }, [id, view.controlled, pauseInput])

  async function command(action: "claim" | "start" | "handoff") {
    if (commandPending.current || !lifetime.current.visible) return
    commandPending.current = true
    changingControl.current = true
    pauseInput()
    setWorking(true); setError("")
    lifetime.current.invalidate()
    try {
      await controlComputer({ path: { session_id: id }, body: { action, client_id: clientId.current } })
      owned.current = action !== "handoff"
      if (!mounted.current) {
        // Setup may have completed after unmount's first release attempt.
        owned.current = action !== "handoff"; releaseOwned()
        return
      }
      setView(previous => ({ ...previous, controlled: owned.current, can_release: owned.current }))
    } catch (error) {
      if (mounted.current) setError(String(error))
    } finally {
      changingControl.current = false; commandPending.current = false
      if (mounted.current) {
        setWorking(false)
        void refresh().catch(() => {})
      }
    }
  }

  function paste(text: string, target = remote.current) {
    if (!target || target !== remote.current) return
    try { pasteRemote(target, text, lifetime.current.visible && view.controlled && connected && !commandPending.current) }
    catch (error) { setError(String(error)) }
  }

  function key(event: React.KeyboardEvent, down: boolean) {
    if (!lifetime.current.visible || !view.controlled || !connected || commandPending.current) return
    if (down) heldKeys.current.set(event.code, { key: event.key, code: event.code, location: event.location })
    if (!down) heldKeys.current.delete(event.code)
    if (event.key === "Control" || event.key === "Meta") {
      if (down) heldModifiers.current.add(event.code)
      else heldModifiers.current.delete(event.code)
      if (!down && !heldModifiers.current.size) remote.current?.sendKey(CONTROL, "ControlLeft", false)
    }
    if (down && event.ctrlKey && event.shiftKey && event.key === "Escape") {
      event.preventDefault(); event.stopPropagation(); void command("handoff"); return
    }
    // The remote desktop runs Linux. Map Mac Command to Control before noVNC
    // handles the remaining key, keeping browser shortcuts useful.
    if (event.key === "Meta") {
      event.preventDefault(); event.stopPropagation()
      remote.current?.sendKey(CONTROL, "ControlLeft", down || heldModifiers.current.size > 0)
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") {
      // Let the browser deliver its ordinary paste event. noVNC must not send
      // Ctrl+V before that event has supplied the user's clipboard contents.
      event.stopPropagation()
      if (down && !event.repeat) pasteReceiver.current?.focus({ preventScroll: true })
    }
  }

  const canInput = foreground && view.controlled && connected && !working
  const occupied = Boolean(view.controller && !view.controlled)
  return <div className="computer-panel">
    {(view.controlled || view.can_release) && <div className="desktop-control-bar" role="group" aria-label="Computer controls">
      <span>{view.controlled ? "You have control" : "You have control in another tab"}</span>
      <Button variant="secondary" size="sm" className="desktop-release-control"
        disabled={!foreground || working} title="Release control (Ctrl+Shift+Escape)"
        onClick={() => void command("handoff")}><MousePointer2 size={14} />Release control</Button>
    </div>}
    <div className={`desktop-stage ${canInput ? "controlled" : ""}`} role="application" aria-label="Sandbox desktop"
      onMouseDownCapture={event => {
        if (!canInput) return
        pointer.current.buttons.add(event.button); pointer.current.x = event.clientX; pointer.current.y = event.clientY
      }}
      onMouseMoveCapture={event => { pointer.current.x = event.clientX; pointer.current.y = event.clientY }}
      onMouseUpCapture={event => { pointer.current.buttons.delete(event.button) }}
      onKeyDownCapture={event => key(event, true)} onKeyUpCapture={event => key(event, false)}
      onPasteCapture={event => {
        if (!canInput) return
        event.preventDefault(); event.stopPropagation(); paste(event.clipboardData.getData("text/plain"))
      }}
      onBlurCapture={event => {
        if (event.relatedTarget instanceof Node && event.currentTarget.contains(event.relatedTarget)) return
        if (remote.current) releaseRemoteKeys(remote.current, heldKeys.current, key => {
          host.current?.querySelector("canvas")?.dispatchEvent(new KeyboardEvent("keyup", { ...key, bubbles: true }))
        })
        heldModifiers.current.clear()
      }}>
      <div className="desktop-stream" ref={host} />
      <textarea ref={pasteReceiver} className="desktop-keyboard" aria-hidden="true" tabIndex={-1} autoComplete="off" />
      {connected && !view.controlled && <Button variant="ghost" className="desktop-take-control" disabled={!foreground || working || occupied}
        onClick={() => void command("claim")} aria-label={occupied ? `${view.controller} has control` : "Take control of computer"}>
        <span className="desktop-control-prompt"><MousePointer2 size={32} /><strong>{working ? "Taking control…" : occupied ? `${view.controller} has control` : "Take control"}</strong>
          {!occupied && <span>Click anywhere to use the computer</span>}</span>
      </Button>}
      {!connected && <div className="desktop-empty"><Monitor size={32} />
        <p role="status">{working ? "Opening computer…" : connecting ? "Connecting…" : hasWorkspace ? "Your sandbox desktop" : "Send Moyai a task to start its computer."}</p>
        {hasWorkspace && <Button disabled={!foreground || working || connecting || Boolean(view.controller && !view.available && !view.controlled)} onClick={() => view.available ? setAttempt(value => value + 1) : void command("start")}>{working ? "Preparing desktop…" : view.available ? <><RefreshCw size={15} />Reconnect</> : "Open computer"}</Button>}
      </div>}
      {error && <div className="desktop-notice" role="alert"><span>{error}</span><Button variant="ghost" size="icon-sm" aria-label="Dismiss computer error" onClick={() => setError("")}><X size={14} /></Button></div>}
    </div>
  </div>
}
