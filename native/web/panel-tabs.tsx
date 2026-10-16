import { useEffect, useRef, useState, type ReactNode } from "react"
import { Tabs as TabsPrimitive } from "radix-ui"
import { Activity, CheckSquare, File, Files, GitPullRequest, Maximize2, MessageSquare, Minimize2, Monitor, PanelRightClose, Plus, Search, Sparkles, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import type { WorkspaceTab } from "./workspace-types"
import "./panel-tabs.css"

export type PanelState = { tabs: WorkspaceTab[]; activeId: string | null; visible: boolean; expanded: boolean }
export const emptyPanelState: PanelState = { tabs: [], activeId: null, visible: false, expanded: false }
export type PanelAction =
  | { type: "open"; tab: WorkspaceTab }
  | { type: "replace"; id: string; tab: WorkspaceTab }
  | { type: "rename"; id: string; title: string }
  | { type: "select"; id: string }
  | { type: "close"; id: string }
  | { type: "hide" }
  | { type: "show" }
  | { type: "expand" }

export function panelReducer(state: PanelState, action: PanelAction): PanelState {
  if (action.type === "hide") return { ...state, visible: false, expanded: false }
  if (action.type === "show") return { ...state, visible: true }
  if (action.type === "expand") return { ...state, expanded: !state.expanded }
  if (action.type === "rename") return state.tabs.some(tab => tab.id === action.id && tab.title !== action.title)
    ? { ...state, tabs: state.tabs.map(tab => tab.id === action.id ? { ...tab, title: action.title } : tab) } : state
  if (action.type === "select") return state.tabs.some(tab => tab.id === action.id)
    ? { ...state, activeId: action.id, visible: true } : state
  if (action.type === "close") {
    const index = state.tabs.findIndex(tab => tab.id === action.id)
    if (index < 0) return state
    const tabs = state.tabs.filter(tab => tab.id !== action.id)
    return {
      ...state, tabs, activeId: state.activeId === action.id ? tabs[Math.min(index, tabs.length - 1)]?.id ?? null : state.activeId,
      visible: state.visible && tabs.length > 0, expanded: state.expanded && tabs.length > 0,
    }
  }
  if (action.type === "replace") {
    if (!state.tabs.some(tab => tab.id === action.id)) return state
    const alreadyOpen = state.tabs.some(tab => tab.id === action.tab.id && tab.id !== action.id)
    return {
      ...state,
      tabs: alreadyOpen ? state.tabs.filter(tab => tab.id !== action.id) : state.tabs.map(tab => tab.id === action.id ? action.tab : tab),
      activeId: state.activeId === action.id ? action.tab.id : state.activeId,
    }
  }
  const tabs = state.tabs
  const existing = tabs.findIndex(tab => tab.id === action.tab.id)
  return {
    ...state, tabs: existing < 0 ? [...tabs, action.tab] : tabs.map(tab => tab.id === action.tab.id ? action.tab : tab),
    activeId: action.tab.id, visible: true,
  }
}

const icons = {
  "pull-request": GitPullRequest, subagent: Sparkles, output: File, computer: Monitor,
  activity: Activity, plan: CheckSquare, files: Files, "side-chat": MessageSquare,
}
export type AddTabKind = "side-chat" | "files" | "computer" | "activity" | "plan"
const choices: { kind: AddTabKind; label: string; description: string }[] = [
  { kind: "side-chat", label: "Side chat", description: "Open a separate conversation beside this one" },
  { kind: "files", label: "Files", description: "Saved outputs and attachments" },
  { kind: "computer", label: "Computer", description: "Your session’s desktop and browser" },
  { kind: "activity", label: "Activity", description: "Recorded tool calls and progress" },
  { kind: "plan", label: "Plan", description: "Tasks and their status" },
]

export function AddTabMenu({ onAdd }: { onAdd: (kind: AddTabKind) => void }) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState("")
  const filtered = choices.filter(item => `${item.label} ${item.description}`.toLowerCase().includes(query.toLowerCase()))
  return <Popover open={open} onOpenChange={value => { setOpen(value); if (value) setQuery("") }}>
    <PopoverTrigger asChild><Button variant="ghost" size="icon-sm" aria-label="Add panel tab" title="Add tab"><Plus size={17} /></Button></PopoverTrigger>
    <PopoverContent className="panel-add-menu" align="end" collisionPadding={12}>
      <label className="panel-add-search"><Search size={16} /><Input autoFocus aria-label="Search panel tabs" placeholder="Search…" value={query} onChange={event => setQuery(event.target.value)}
        onKeyDown={event => { if (event.key === "ArrowDown") { event.preventDefault(); event.currentTarget.closest("[data-slot=popover-content]")?.querySelector<HTMLButtonElement>('[role="menuitem"]')?.focus() } }} /></label>
      <div role="menu" aria-label="Add a panel tab" onKeyDown={event => {
        if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return
        event.preventDefault()
        const buttons = [...event.currentTarget.querySelectorAll<HTMLButtonElement>('[role="menuitem"]')]
        const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
        buttons[event.key === "Home" ? 0 : event.key === "End" ? buttons.length - 1 : (index + (event.key === "ArrowDown" ? 1 : -1) + buttons.length) % buttons.length]?.focus()
      }}>{filtered.map(item => { const Icon = icons[item.kind]; return <Button role="menuitem" key={item.kind} variant="ghost" onClick={() => { setOpen(false); onAdd(item.kind) }}><Icon size={17} /><span>{item.label}<small>{item.description}</small></span></Button> })}</div>
      {!filtered.length && <p>No matching tabs.</p>}
    </PopoverContent>
  </Popover>
}

export function PanelTabs({ state, dispatch, onAdd, children }: {
  state: PanelState; dispatch: (action: PanelAction) => void; onAdd: (kind: AddTabKind) => void
  children: (tab: WorkspaceTab, active: boolean) => ReactNode
}) {
  const tablist = useRef<HTMLDivElement>(null)
  const wasVisible = useRef(false)
  useEffect(() => {
    const target = state.visible
      ? tablist.current?.querySelector<HTMLElement>('[aria-selected="true"]')
      : wasVisible.current ? document.querySelector<HTMLElement>('.workspace-toggle') : null
    wasVisible.current = state.visible
    if (!target) return
    target.scrollIntoView({ block: "nearest", inline: "nearest" })
    const frame = requestAnimationFrame(() => target.focus({ preventScroll: true }))
    return () => cancelAnimationFrame(frame)
  }, [state.activeId, state.visible])
  const selected = Math.max(0, state.tabs.findIndex(tab => tab.id === state.activeId))
  return <TabsPrimitive.Root asChild value={state.activeId ?? ""} onValueChange={id => dispatch({ type: "select", id })}><aside className="details-panel tabbed-panel" aria-label="Session panel" hidden={!state.visible}>
    <div className="panel-tabbar">
      <TabsPrimitive.List ref={tablist} className="panel-tablist" aria-label="Open panel tabs" onKeyDown={event => {
        if (!(event.target instanceof HTMLElement) || event.target.getAttribute("role") !== "tab") return
        if (event.key !== "Delete") return
        event.preventDefault()
        const next = state.tabs[selected + 1] ?? state.tabs[selected - 1]
        dispatch({ type: "close", id: state.activeId! })
        requestAnimationFrame(() => next ? tablist.current?.querySelector<HTMLButtonElement>(`[data-tab-id="${CSS.escape(next.id)}"]`)?.focus() : document.querySelector<HTMLButtonElement>('.workspace-toggle')?.focus())
      }}>
        {state.tabs.map(tab => { const Icon = icons[tab.kind]; return <div key={tab.id} className={`panel-tab ${state.activeId === tab.id ? "selected" : ""}`}>
          <TabsPrimitive.Trigger value={tab.id} data-tab-id={tab.id} title={tab.title}><Icon size={15} /><span>{tab.title}</span></TabsPrimitive.Trigger>
          <Button className="panel-tab-close" variant="ghost" size="icon-sm" aria-label={`Close ${tab.title} tab`} onClick={() => dispatch({ type: "close", id: tab.id })}><X size={13} /></Button>
        </div> })}
      </TabsPrimitive.List>
      <div className="panel-tab-actions"><AddTabMenu onAdd={onAdd} /><Button variant="ghost" size="icon-sm" aria-label={state.expanded ? "Restore panel size" : "Expand side panel"} title={state.expanded ? "Restore size" : "Expand panel"} onClick={() => dispatch({ type: "expand" })}>{state.expanded ? <Minimize2 size={15} /> : <Maximize2 size={15} />}</Button>
        <Button variant="ghost" size="icon-sm" aria-label="Hide side panel" title="Hide panel" onClick={() => dispatch({ type: "hide" })}><PanelRightClose size={16} /></Button></div>
    </div>
    {!state.tabs.length && <div className="panel-empty"><Files /><p>Use + to open a side chat, files, or your computer.</p></div>}
    {state.tabs.map(tab => <TabsPrimitive.Content forceMount value={tab.id} key={tab.id} className={`panel-tab-content tab-${tab.kind}`} hidden={!state.visible || state.activeId !== tab.id}>
      {children(tab, state.visible && state.activeId === tab.id)}
    </TabsPrimitive.Content>)}
  </aside></TabsPrimitive.Root>
}
