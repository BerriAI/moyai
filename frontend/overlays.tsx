import React, { useLayoutEffect, useRef, useState } from "react"
import { createRoot } from "react-dom/client"
import { flushSync } from "react-dom"
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog"
import { Popover, PopoverAnchor, PopoverContent } from "@/components/ui/popover"
import { dispose, render } from "./render"

export type DialogHost = HTMLDivElement & {
  open: boolean
  returnValue: string
  showModal: () => void
  close: (value?: string) => void
}
type PopoverHost = HTMLDivElement & {
  showPopover: (options?: { source?: HTMLElement }) => void
  hidePopover: () => void
}

function OwnedContent({ host }: { host: HTMLElement }) {
  const ref = useRef<HTMLDivElement>(null)
  useLayoutEffect(() => {
    ref.current!.append(host)
    host.hidden = false
    return () => { host.hidden = true; document.body.append(host) }
  }, [host])
  return <div ref={ref} style={{ display: "contents" }} />
}

function eventProperty(host: HTMLElement, name: "close" | "cancel") {
  let handler: EventListener | null = null
  Object.defineProperty(host, "on" + name, {
    configurable: true,
    get: () => handler,
    set: (next: EventListener | null) => {
      if (handler) host.removeEventListener(name, handler)
      handler = next
      if (handler) host.addEventListener(name, handler)
    },
  })
}

export function createDialog(): DialogHost {
  const host = document.createElement("div") as DialogHost
  host.dataset.dialogBody = "true"
  host.hidden = true
  host.returnValue = ""
  document.body.append(host)
  const mount = document.createElement("div")
  mount.dataset.overlayRoot = "true"
  document.body.append(mount)
  const root = createRoot(mount)
  let changeOpen: (value: boolean) => void = () => {}
  let open = false
  let trigger: HTMLElement | null = null
  eventProperty(host, "close")
  eventProperty(host, "cancel")
  Object.defineProperty(host, "open", { get: () => open })
  host.showModal = () => {
    if (open) return
    // Dialog styling follows its owner, independently of the current route.
    mount.classList.toggle("settings-view", host.dataset.dialogScope === "settings")
    trigger = document.activeElement as HTMLElement
    open = true
    host.returnValue = ""
    flushSync(() => changeOpen(true))
  }
  host.close = (value = "") => {
    if (!open) return
    open = false
    host.returnValue = value
    flushSync(() => changeOpen(false))
    host.dispatchEvent(new Event("close"))
  }
  const remove = host.remove.bind(host)
  host.remove = () => {
    if (open) host.close()
    dispose(host)
    flushSync(() => root.unmount())
    mount.remove()
    remove()
  }
  function Frame() {
    const [visible, setVisible] = useState(false)
    changeOpen = setVisible
    const cancel = (event: Event) => {
      // A controller's Select has its own React root. During portal mount,
      // Escape/outside clicks still belong to that menu, not the form beneath it.
      if (host.querySelector('[data-slot="select-trigger"][data-state="open"]')) {
        event.preventDefault()
        return
      }
      if (!host.dispatchEvent(new Event("cancel", { cancelable: true }))) event.preventDefault()
    }
    const label = host.getAttribute("aria-label") || host.querySelector("h1,h2,h3,strong")?.textContent || "Dialog"
    return <Dialog open={visible} onOpenChange={next => { if (!next) host.close() }}>
      {visible && <DialogContent
        container={mount}
        className={host.className}
        data-dialog-id={host.id}
        showCloseButton={false}
        aria-describedby={host.getAttribute("aria-describedby") || undefined}
        onEscapeKeyDown={cancel}
        onPointerDownOutside={cancel}
        onCloseAutoFocus={event => { event.preventDefault(); if (trigger?.isConnected) trigger.focus() }}
      >
        <DialogTitle asChild><span className="sr-only">{label}</span></DialogTitle>
        <OwnedContent host={host} />
      </DialogContent>}
    </Dialog>
  }
  flushSync(() => root.render(<Frame />))
  return host
}

export function createPopover(id: string): PopoverHost {
  const host = document.createElement("div") as PopoverHost
  host.id = id
  host.hidden = true
  host.dataset.popoverBody = "true"
  document.body.append(host)
  const mount = document.createElement("div")
  mount.dataset.overlayRoot = "true"
  document.body.append(mount)
  const root = createRoot(mount)
  let change: (open: boolean) => void = () => {}
  let source: HTMLElement | null = null
  const virtualRef = { current: { getBoundingClientRect: () => source?.getBoundingClientRect() || new DOMRect() } }
  host.showPopover = (options?: { source?: HTMLElement }) => { source = options?.source || document.activeElement as HTMLElement; host.dataset.open = "true"; flushSync(() => change(true)) }
  host.hidePopover = () => { host.dataset.open = "false"; flushSync(() => change(false)) }
  function Frame() {
    const [open, setOpen] = useState(false)
    change = setOpen
    return <Popover open={open} onOpenChange={next => { if (!next) host.hidePopover() }}>
      <PopoverAnchor virtualRef={virtualRef} />
      {open && <PopoverContent data-popover-id={id} side="right" align="start" collisionPadding={8}
        onOpenAutoFocus={event => event.preventDefault()}
        onCloseAutoFocus={event => { event.preventDefault(); if (source?.isConnected && !document.querySelector('[data-slot="dialog-content"]')) source.focus() }}>
        <OwnedContent host={host} />
      </PopoverContent>}
    </Popover>
  }
  flushSync(() => root.render(<Frame />))
  return host
}

export function initializeShell() {
  const dialogs = [...document.querySelectorAll("dialog")].map(node => {
    const value = { attributes: [...node.attributes], html: node.innerHTML }
    node.remove()
    return value
  })
  document.querySelector("#session-actions")?.remove()
  render(document.body, document.body.innerHTML)
  for (const { attributes, html } of dialogs) {
    const host = createDialog()
    for (const attribute of attributes) host.setAttribute(attribute.name, attribute.value)
    render(host, html)
  }
  createPopover("session-actions")
}
