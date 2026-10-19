import React, { useCallback, useEffect, useId, useRef, useState } from "react"
import { AlertCircle, CalendarClock, ChevronLeft, History, Plus, RefreshCw, Search, Trash2 } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { automationDefaultSchedule, automationEventFields, automationSetupNotes, automationSources, automationTriggers } from "./automation-helpers.js"
import { automationHistoryMerge, automationRunStatus, automationTiming } from "./automation-helpers.js"
import { api, ApiError, csrfToken } from "./api"
import {
  configureProductAutomationWebhook, createProductAutomation, getMe, getProductAutomationPreview, getProductAutomationWebhook,
  listProductAutomationHistory, listProductAutomations, migrateProductAutomation, runProductAutomation, setProductAutomationState,
  testProductAutomationEvent, updateProductAutomation,
} from "./generated/api"
import {
  AutomationRequestKeys, automationActions, automationBlocker, automationMetadata, automationSamplePayload, automationStatus, calendarFrequencies, changeAutomationEvent, changeAutomationEventField, changeAutomationRepository, eventFieldVisible, historySessionHash, isAutomationProvider,
  onceUtcInput, onceUtcInstant, validateAutomation,
  type AutomationDefinition, type AutomationHistoryPage, type AutomationInventory, type AutomationPreview,
  type AutomationTiming, type AutomationTrigger, type AutomationEvent, type ProductAutomation, type AutomationRunResult,
  type AutomationWebhookResult, type AutomationEnvironment, type AutomationConnection,
} from "./product-automation-data"
import "./product-automations.css"

const base = "/product/automations"
const timeLabel = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not recorded"
const errorLabel = (reason: unknown) => reason instanceof ApiError && reason.status === 404
  ? "This automation service or saved definition is not available."
  : reason instanceof Error ? reason.message : "The request could not be completed."
/** `path` identifies the resource; `load` fetches it (legacy `api<T>(path)` when omitted). */
function useRead<T>(path: string | null, load?: (signal: AbortSignal) => Promise<T>) {
  const loader = useRef(load); loader.current = load
  const [version, setVersion] = useState(0)
  const [state, setState] = useState<{ path: string | null; data?: T; loading: boolean; error: string }>({ path: null, loading: false, error: "" })
  useEffect(() => {
    if (!path) return
    const controller = new AbortController()
    const requestAuth = csrfToken()
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true, error: "" }))
    void (loader.current ? loader.current(controller.signal) : api<T>(path, "GET", undefined, controller.signal)).then(data => {
      if (!controller.signal.aborted && csrfToken() === requestAuth) setState({ path, data, loading: false, error: "" })
    }, error => { if (!controller.signal.aborted && csrfToken() === requestAuth) setState({ path, loading: false, error: errorLabel(error) }) })
    return () => controller.abort()
  }, [path, version])
  const refresh = useCallback(() => setVersion(value => value + 1), [])
  return { ...(state.path === path ? state : { path, loading: Boolean(path), error: "", data: undefined }), refresh }
}
function Choice({ value, onChange, label, options, disabled, id }: {
  value: string; onChange: (value: string) => void; label: string; options: { value: string; label: string }[]; disabled?: boolean; id?: string
}) {
  return <Select value={value} onValueChange={onChange} disabled={disabled}><SelectTrigger id={id} aria-label={label}><SelectValue /></SelectTrigger>
    <SelectContent className="native-settings-select">{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
  </Select>
}
function TriggerSummary({ trigger }: { trigger: AutomationTrigger }) {
  if (trigger.event) return <span>{automationSources[trigger.event.provider] ?? trigger.event.provider} · {trigger.event.event}</span>
  if (!trigger.schedule) return <span>Schedule unavailable</span>
  return <span>{automationTiming(trigger.schedule)}{trigger.schedule.frequency === "once" ? ` · ${Intl.DateTimeFormat().resolvedOptions().timeZone || "local time"}` : ""}</span>
}
export function ProductAutomationList({ rows, onReview }: { rows: ProductAutomation[]; onReview: (id: string) => void }) {
  return <div className="ns-records pa-list">{rows.map(row => <article className="ns-record pa-record" key={row.id}>
    <CalendarClock className="ns-record-icon" aria-hidden="true" /><div className="ns-record-copy">
      <div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{automationStatus(row)}</span></div>
      <ul className="pa-triggers">{row.triggers.map(trigger => <li key={trigger.id}><TriggerSummary trigger={trigger} /></li>)}</ul>
      {!row.native && <p className="ns-muted">Review migration before this workflow can run on the new runtime.</p>}
      {row.owned === false && <p className="ns-muted">Shared definition · Owner-managed</p>}
      {row.native?.last_error && <p className="pa-warning" role="status">{row.native.last_error}</p>}
    </div><div className="ns-row-actions"><Button variant="outline" size="sm" onClick={() => onReview(row.id)}>Review<span className="visually-hidden"> {row.name}</span></Button></div>
  </article>)}</div>
}

/** This page reads canonical product definitions. Temporal owns all scheduling. */
export function ProductAutomationsPage({ userId }: { userId: string }) {
  const [pages, setPages] = useState(1), [version, setVersion] = useState(0)
  const [state, setState] = useState<{ data?: AutomationInventory; loading: boolean; error: string }>({ loading: true, error: "" })
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [dialog, setDialog] = useState<{ id?: string; mode: "new" | "review" | "edit" } | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const refresh = useCallback(() => setVersion(value => value + 1), [])
  useEffect(() => {
    const revoked = () => { setDialog(null); setState({ loading: true, error: "" }); refresh() }
    window.addEventListener("moyai:access-changed", revoked)
    return () => window.removeEventListener("moyai:access-changed", revoked)
  }, [refresh])
  useEffect(() => {
    const controller = new AbortController()
    const requestAuth = csrfToken()
    setState(previous => ({ ...previous, loading: true, error: "" }))
    void (async () => {
      try {
        let offset = 0, result: AutomationInventory | undefined
        const rows = new Map<string, ProductAutomation>()
        for (let page = 0; page < pages; page++) {
          const { data: next } = await listProductAutomations({ query: { limit: 50, offset }, signal: controller.signal })
          if (controller.signal.aborted || csrfToken() !== requestAuth) return
          if (next.source !== "canonical_product") throw new Error("The server did not return canonical product automations.")
          for (const row of next.automations) rows.set(row.id, row)
          result = { ...next, automations: [...rows.values()] }
          if (next.next_offset === null || next.next_offset <= offset) break
          offset = next.next_offset
        }
        if (!controller.signal.aborted && csrfToken() === requestAuth) setState({ data: result, loading: false, error: "" })
      } catch (reason) { if (!controller.signal.aborted && csrfToken() === requestAuth) { setState({ loading: false, error: errorLabel(reason) }); setDialog(null) } }
    })()
    return () => controller.abort()
  }, [userId, pages, version])
  const open = (mode: "new" | "review", id?: string) => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setDialog({ mode, id }) }
  const rows = state.data?.automations.filter(row => `${row.name} ${row.triggers.map(trigger => trigger.event?.provider ?? trigger.schedule?.frequency ?? "").join(" ")}`.toLowerCase().includes(search.toLowerCase()) && (
    filter === "all" || filter === "unmigrated" && !row.native || filter === "active" && row.native?.state === "active"
    || filter === "paused" && (row.native ? row.native.state === "paused" : row.legacy_paused)
    || filter === "blocked" && row.native?.sync_state === "blocked"
  )) ?? []
  return <section className="product-automations" aria-label="Saved product automations">
    <p className="ns-notice"><AlertCircle size={16} />Existing definitions and run history stay saved. Migration is explicit and starts paused. Definitions and scheduled sessions are shared workspace resources.</p>
    <div className="ns-toolbar">
      <div className="ns-search"><Search size={16} /><Input type="search" aria-label="Search automations" placeholder="Search automations…" value={search} onChange={event => setSearch(event.target.value)} /></div>
      <Choice label="Automation state" value={filter} onChange={setFilter} options={[{ value: "all", label: "All states" }, { value: "unmigrated", label: "Not migrated" }, { value: "active", label: "Enabled" }, { value: "paused", label: "Paused" }, { value: "blocked", label: "Blocked" }]} />
      <Button variant="outline" disabled={state.loading} onClick={refresh}><RefreshCw size={16} />Refresh</Button>
      {state.data?.capabilities?.create === true && <Button onClick={() => open("new")}><Plus size={16} />Create automation</Button>}
    </div>
    {state.loading && <p className="ns-muted" role="status">Loading saved automations…</p>}
    {state.error && <p className="ns-error" role="alert">{state.error} <Button variant="outline" onClick={refresh}>Retry</Button></p>}
    {state.data && <><p className="ns-muted">{rows.length} shown{state.data.next_offset !== null ? " · More workflows available" : ""}</p>
      {rows.length ? <ProductAutomationList rows={rows} onReview={id => open("review", id)} /> : <div className="ns-empty"><h2>{search || filter !== "all" ? "No matching automations" : "No saved automations"}</h2><p>{search || filter !== "all" ? "Try another search or clear the filters." : "Create a calendar workflow and review it before migration and activation."}</p>{(search || filter !== "all") && <Button variant="outline" onClick={() => { setSearch(""); setFilter("all") }}>Clear filters</Button>}</div>}
      {state.data.next_offset !== null && <Button className="pa-load-more" variant="outline" disabled={state.loading} onClick={() => setPages(value => value + 1)}>Load more automations</Button>}
    </>}
      {dialog && <AutomationDialog key={dialog.id ?? "new"} mode={dialog.mode} id={dialog.id} canCreate={state.data?.capabilities?.create === true} eventChoices={state.data?.event_choices ?? {}}
      onMode={mode => setDialog(current => current ? { ...current, mode } : null)} onSaved={refresh} onClose={() => setDialog(null)} opener={opener} />}
  </section>
}

export function AutomationReadiness({ preview }: { preview: AutomationPreview }) {
  return <div className="pa-readiness">
    <div className="ns-record-title"><h3>Migration readiness</h3><span className="ns-badge">{preview.supported && !preview.blockers.length ? "Supported definition" : "Blocked"}</span></div>
    <p>{preview.sharing_notice || "Definitions and scheduled sessions are shared workspace resources."}</p>
    {preview.blockers.length > 0 && <ul className="pa-blockers">{preview.blockers.map(code => <li key={code}>{automationBlocker(code)}</li>)}</ul>}
    {preview.native?.last_error && <p className="pa-warning" role="status">{preview.native.last_error}</p>}
    {preview.native && <p className="ns-muted">{automationStatus(preview)}{preview.native.sync_state !== "ready" ? " · Refresh to check scheduler synchronization." : ""}</p>}
    <p className="ns-muted">Readiness checks the saved triggers, account access, repository and environment setup, and permissions. Resolve reported blockers before activation.</p>
  </div>
}
function HistoryRows({ id, version }: { id: string; version: number }) {
  const [cursor, setCursor] = useState<string | null>(null), [saved, setSaved] = useState<AutomationHistoryPage["history"]>([])
  const resource = useRead(`${base}/${encodeURIComponent(id)}/history?limit=20${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ""}`,
    signal => listProductAutomationHistory({ path: { identity: id }, query: { limit: 20, ...cursor ? { cursor } : {} }, signal }).then(({ data }) => data))
  useEffect(() => { setCursor(null); setSaved([]); resource.refresh() }, [id, version])
  useEffect(() => { if (resource.error) setSaved([]) }, [resource.error])
  const rows = resource.error ? [] : resource.data ? automationHistoryMerge(saved, resource.data.history) : saved
  return <section className="pa-history"><h3><History size={16} />Run history</h3>
    {resource.error && <p className="ns-error" role="alert">{resource.error}<Button variant="outline" onClick={resource.refresh}>Retry history</Button></p>}
    {resource.loading && <p className="ns-muted" role="status">Loading history…</p>}
    {rows.length > 0 && <div className="ns-table" role="region" aria-label="Automation run history" tabIndex={0}><Table><TableHeader><TableRow><TableHead>Started</TableHead><TableHead>State</TableHead><TableHead>Session</TableHead></TableRow></TableHeader><TableBody>{rows.map(row => {
      const href = historySessionHash(row)
      return <TableRow key={row.occurrence}><TableCell>{timeLabel(row.created_at)}<small>{row.engine === "legacy" ? "Saved legacy run" : row.engine}</small></TableCell><TableCell>{automationRunStatus(row)}{row.detail && <small>{row.detail}</small>}</TableCell><TableCell>{href ? <a className="ns-link" href={href}>Open session</a> : "No session admitted"}</TableCell></TableRow>
    })}</TableBody></Table></div>}
    {!resource.loading && !resource.error && !rows.length && <p className="ns-muted">No recorded executions.</p>}
    {resource.data?.next_cursor && <Button variant="outline" disabled={resource.loading} onClick={() => { setSaved(rows); setCursor(resource.data!.next_cursor) }}>Load earlier runs</Button>}
  </section>
}

function EventTools({ preview, busy, version, onSetup }: {
  preview: AutomationPreview; busy: boolean; version: number
  onSetup: (provider: string, secret: string, done: (result: AutomationWebhookResult) => void) => Promise<void>
}) {
  const resource = useRead(`${base}/${encodeURIComponent(preview.id)}/webhook`,
    signal => getProductAutomationWebhook({ path: { identity: preview.id }, signal }).then(({ data }) => data))
  const [provider, setProvider] = useState(""), [secret, setSecret] = useState(""), [generated, setGenerated] = useState("")
  const [setupNotice, setSetupNotice] = useState(""), [sampleIndex, setSampleIndex] = useState("0")
  const [payload, setPayload] = useState<string | null>(null), [eventHeader, setEventHeader] = useState<string | null>(null)
  const [testResult, setTestResult] = useState(""), [testError, setTestError] = useState(""), [testing, setTesting] = useState(false)
  const request = useRef<AbortController | null>(null), secretOutput = useRef<HTMLInputElement>(null)
  const id = useId()
  useEffect(() => { resource.refresh() }, [version])
  useEffect(() => () => request.current?.abort(), [])
  useEffect(() => {
    const clear = () => { setSecret(""); setGenerated(""); setPayload(null); setTestResult(""); resource.refresh(); request.current?.abort() }
    window.addEventListener("moyai:access-changed", clear)
    return () => window.removeEventListener("moyai:access-changed", clear)
  }, [resource.refresh])
  const receivers = resource.data?.providers.filter(row => !["session", "slack"].includes(row.provider)) ?? []
  const selected = receivers.find(row => row.provider === provider) ?? receivers[0]
  const samples = resource.data?.providers.flatMap(row => row.examples.map(sample => ({ ...sample, provider: row.provider }))) ?? []
  const sample = samples[Number(sampleIndex)] ?? samples[0]
  const currentPayload = payload ?? (sample ? JSON.stringify(sample.payload, null, 2) : "")
  const requiredSecret = selected && ["linear", "pagerduty"].includes(selected.provider)
  return <section className="pa-event-tools"><h3>Event sources</h3>
    {resource.loading && <p className="ns-muted" role="status">Loading receiver configuration…</p>}
    {resource.error && <p className="ns-error" role="alert">{resource.error}<Button variant="outline" onClick={resource.refresh}>Retry event sources</Button></p>}
    {resource.data && <ul className="pa-triggers">{resource.data.providers.map(row => <li key={row.provider}>{automationSources[row.provider] || row.provider} · {row.ready ? "Receiver ready" : "Setup required"}</li>)}</ul>}
    {preview.capabilities?.webhook && selected && <details><summary>Configure webhook receiver</summary>
      <form className="ns-fields pa-tool-form" onSubmit={event => {
        event.preventDefault(); setGenerated(""); setSetupNotice("")
        void onSetup(selected.provider, secret, result => {
          setSecret(""); setGenerated(result.secret || "")
          setSetupNotice(result.configured ? "Receiver secret saved. This automation is paused. Register the URL and secret with the provider, then review readiness before enabling." : "The service did not confirm receiver configuration.")
          resource.refresh()
        })
      }}>
        <div className="ns-field"><label htmlFor={`${id}-provider`}>Provider</label><Choice id={`${id}-provider`} label="Webhook provider" value={selected.provider} disabled={busy} options={receivers.map(row => ({ value: row.provider, label: automationSources[row.provider] || row.provider }))} onChange={value => { setProvider(value); setSecret(""); setGenerated(""); setSetupNotice("") }} /></div>
        <div className="ns-field"><label htmlFor={`${id}-url`}>Webhook URL</label><Input id={`${id}-url`} readOnly value={selected.url} onFocus={event => event.currentTarget.select()} /></div>
        <p className="ns-field-hint">{automationSetupNotes[selected.provider]}</p>
        <div className="ns-field"><label htmlFor={`${id}-secret`}>Signing secret{requiredSecret ? "" : " (leave blank to generate)"}</label><Input id={`${id}-secret`} type="password" autoComplete="new-password" required={requiredSecret} minLength={16} maxLength={512} disabled={busy} value={secret} onChange={event => setSecret(event.target.value)} /></div>
        <p className="ns-field-hint">Saving pauses this automation and replaces any previous receiver secret. Provider registration is a separate step.</p>
        <Button type="submit" variant="outline" disabled={busy}>{busy ? "Saving…" : "Save receiver secret"}</Button>
        {setupNotice && <p className="ns-notice" role="status">{setupNotice}</p>}
        {generated && <div className="ns-field"><label htmlFor={`${id}-generated`}>Generated secret · available until this dialog closes</label><Input ref={secretOutput} id={`${id}-generated`} type="password" autoComplete="off" readOnly value={generated} />
          <Button type="button" variant="outline" onClick={async () => {
            try { await navigator.clipboard.writeText(generated); setSetupNotice("Secret copied. Register it with the provider before enabling.") }
            catch { secretOutput.current?.focus(); secretOutput.current?.select(); setSetupNotice("Copy was unavailable. Select and copy the generated secret.") }
          }}>Copy generated secret</Button>
        </div>}
      </form>
    </details>}
    {preview.capabilities?.test_event && sample && <details><summary>Test event filters</summary>
      <form className="ns-fields pa-tool-form" onSubmit={async event => {
        event.preventDefault()
        if (testing || busy) return
        const controller = new AbortController(), requestAuth = csrfToken()
        request.current?.abort(); request.current = controller; setTesting(true); setTestError(""); setTestResult("")
        try {
          const { data: result } = await testProductAutomationEvent({ path: { identity: preview.id }, body: {
            provider: sample.provider, event_header: eventHeader ?? sample.event_header, payload: automationSamplePayload(currentPayload),
          }, signal: controller.signal })
          if (controller.signal.aborted || csrfToken() !== requestAuth) return
          if (result.started !== false) throw new Error("The service did not confirm a filter-only test.")
          setTestResult(result.matches ? `${result.context?.matched_trigger_ids ? `Matched ${result.context.matched_trigger_ids.length} trigger(s).` : "The saved filters matched."} No session was started.` : "No match. No session was started.")
        } catch (reason) {
          if (!controller.signal.aborted && csrfToken() === requestAuth) {
            setTestError(errorLabel(reason))
            if (reason instanceof ApiError && reason.status === 403) window.dispatchEvent(new Event("moyai:access-changed"))
          }
        } finally { if (request.current === controller && csrfToken() === requestAuth) setTesting(false) }
      }}>
        <p className="ns-field-hint">Checks the saved filters against a sample without starting a session.</p>
        <div className="ns-field"><label htmlFor={`${id}-sample`}>Sample</label><Choice id={`${id}-sample`} label="Sample event" value={String(samples.indexOf(sample))} disabled={busy || testing} options={samples.map((value, index) => ({ value: String(index), label: `${automationSources[value.provider] || value.provider} · Trigger ${preview.definition.triggers.findIndex(trigger => trigger.id === value.trigger_id) + 1}` }))} onChange={value => { setSampleIndex(value); setPayload(null); setEventHeader(null); setTestResult(""); setTestError("") }} /></div>
        {sample.provider === "github" && <div className="ns-field"><label htmlFor={`${id}-header`}>GitHub event header</label><Input id={`${id}-header`} value={eventHeader ?? sample.event_header} maxLength={80} disabled={busy || testing} onChange={event => { setEventHeader(event.target.value); setTestResult("") }} /></div>}
        <div className="ns-field"><label htmlFor={`${id}-payload`}>Sample event JSON</label><Textarea id={`${id}-payload`} rows={9} required disabled={busy || testing} value={currentPayload} onChange={event => { setPayload(event.target.value); setTestResult("") }} /></div>
        <Button type="submit" variant="outline" disabled={busy || testing}>{testing ? "Testing…" : "Test filters"}</Button>
        {testResult && <p className="ns-notice" role="status">{testResult}</p>}
        {testError && <p className="ns-error" role="alert">{testError}</p>}
      </form>
    </details>}
    {Boolean(resource.data?.deliveries.length) && <details><summary>Recent event deliveries</summary><div className="ns-table" role="region" aria-label="Recent event deliveries" tabIndex={0}><Table><TableHeader><TableRow><TableHead>Received</TableHead><TableHead>Status</TableHead><TableHead>Detail</TableHead></TableRow></TableHeader><TableBody>
      {resource.data!.deliveries.map((row, index) => <TableRow key={`${row.received_at}:${index}`}><TableCell>{timeLabel(row.received_at)}</TableCell><TableCell>{row.status}</TableCell><TableCell>{row.detail || "—"}</TableCell></TableRow>)}
    </TableBody></Table></div></details>}
  </section>
}

function AutomationDialog({ id, mode, canCreate, eventChoices, onMode, onSaved, onClose, opener }: {
  id?: string; mode: "new" | "review" | "edit"; canCreate: boolean; onMode: (mode: "new" | "review" | "edit") => void
  eventChoices: Record<string, [string, string][]>
  onSaved: () => void; onClose: () => void; opener: React.MutableRefObject<HTMLElement | null>
}) {
  const preview = useRead(id ? `${base}/${encodeURIComponent(id)}/preview` : null,
    signal => getProductAutomationPreview({ path: { identity: id! }, signal }).then(({ data }) => data))
  const models = useRead("/me", signal => getMe({ signal }).then(({ data }) => data))
  const environments = useRead<AutomationEnvironment[]>(mode !== "review" ? "/product/environments" : null)
  const connections = useRead<AutomationConnection[]>(mode !== "review" ? "/product/connections" : null)
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [historyVersion, setHistoryVersion] = useState(0)
  const [confirm, setConfirm] = useState<"migrate" | "enable" | "pause" | "run" | "retire" | null>(null)
  const [runResult, setRunResult] = useState<AutomationRunResult | null>(null)
  const pending = useRef(false), alive = useRef(true), keys = useRef(new AutomationRequestKeys())
  const title = useRef<HTMLHeadingElement>(null)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  useEffect(() => { if (mode === "review") title.current?.focus() }, [mode])
  /** `intent` names the change; a retry of the same intent and body reuses its request key. */
  async function write<B extends object, T>(intent: string, body: B, send: (body: B & { request_key: string }) => Promise<{ data: T }>, close: boolean, done?: (result: T) => void) {
    if (pending.current) return
    const requestAuth = csrfToken()
    pending.current = true; setBusy(true); setError("")
    try {
      const { data: result } = await send({ ...body, request_key: keys.current.forIntent(intent, body) })
      if (!alive.current || csrfToken() !== requestAuth) return
      keys.current.complete()
      done?.(result)
      onSaved()
      if (close) onClose()
      else { setConfirm(null); preview.refresh(); setHistoryVersion(value => value + 1) }
    } catch (reason) {
      if (!alive.current || csrfToken() !== requestAuth) return
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) {
        onClose(); onSaved()
        if (reason.status === 403) window.dispatchEvent(new Event("moyai:access-changed"))
        return
      }
      setError(`${errorLabel(reason)}${reason instanceof ApiError && reason.status === 409 ? " Refresh the saved definition and review its current revision before retrying." : " A retry of the same change keeps its request identity."}`)
    } finally { pending.current = false; if (alive.current && csrfToken() === requestAuth) setBusy(false) }
  }
  const ready = preview.data, actions = ready ? automationActions(ready) : null
  const caps = ready?.capabilities
  return <Dialog open onOpenChange={open => { if (!open && !pending.current) onClose() }}><DialogContent className="native-settings-dialog pa-dialog"
    onEscapeKeyDown={event => { if (pending.current) event.preventDefault() }}
    onCloseAutoFocus={event => { event.preventDefault(); if (opener.current?.isConnected) opener.current.focus({ preventScroll: true }) }}>
    <DialogHeader><DialogTitle ref={title} tabIndex={-1}>{mode === "new" ? "Create automation" : mode === "edit" ? "Edit automation" : ready?.definition.name || "Review automation"}</DialogTitle>
      <DialogDescription>{mode === "review" ? "Review the saved definition, migration readiness, and real execution history." : "Save a shared workflow in a paused state. Saving does not enable scheduled execution."}</DialogDescription></DialogHeader>
    {(preview.error || models.error) && <p className="ns-error" role="alert">{preview.error || models.error}<Button variant="outline" onClick={() => { preview.refresh(); models.refresh() }}>Retry</Button></p>}
    {id && !ready && preview.loading && <p role="status">Loading the saved definition…</p>}
    {error && <p className="ns-error" role="alert">{error}</p>}
    {mode === "review" && ready && <>
      <AutomationReadiness preview={ready} />
      <ul className="pa-triggers">{automationTriggers(ready.definition).map(trigger => <li key={trigger.id}><TriggerSummary trigger={trigger} /></li>)}</ul>
      <details><summary>Workflow instructions</summary><pre>{ready.definition.prompt}</pre></details>
      <UnsupportedSettings definition={ready.definition} />
      <div className="ns-row-actions pa-actions">
        {actions?.edit && <Button variant="outline" disabled={busy || preview.loading} onClick={() => { setError(""); onMode("edit") }}>Edit definition</Button>}
        <Button variant="outline" disabled={busy || preview.loading} onClick={() => { setConfirm(null); setError(""); preview.refresh(); setHistoryVersion(value => value + 1) }}><RefreshCw size={16} />Refresh readiness</Button>
        {caps?.migrate && <Button disabled={busy || preview.loading || !actions?.migrate} onClick={() => setConfirm("migrate")}>{ready.native ? "Retry migration paused" : "Migrate paused"}</Button>}
        {ready.native?.state === "paused" && caps?.enable && <Button disabled={busy || preview.loading || !actions?.enable} onClick={() => setConfirm("enable")}>Resume automation</Button>}
        {actions?.pause && <Button variant="outline" disabled={busy || preview.loading} onClick={() => setConfirm("pause")}>Pause</Button>}
        {caps?.run && <Button variant="outline" disabled={busy || preview.loading || !actions?.run} onClick={() => setConfirm("run")}>Run now</Button>}
        {actions?.retire && <Button variant="outline" disabled={busy || preview.loading} onClick={() => setConfirm("retire")}>Retire automation</Button>}
      </div>
      {confirm && <div className="pa-confirm" role="group" aria-label="Confirm automation change">
        <h3>{{ migrate: "Move execution to the new runtime?", enable: "Enable this shared automation?", pause: "Pause future automation work?", run: "Start one shared session now?", retire: "Retire this automation?" }[confirm]}</h3>
        <p>{confirm === "migrate" ? "The service will pause the old schedule and bind this saved definition to the new runtime. It stays paused until you explicitly resume it. Existing history is retained."
          : confirm === "enable" ? "Future occurrences create shared workspace sessions with these instructions. The service rechecks your access and this definition before admission."
            : confirm === "run" ? "This starts one session using the saved definition and connected accounts. Model usage may incur costs. It does not enable recurring execution."
              : confirm === "retire" ? "This removes the native execution binding from future scheduling. The canonical definition and recorded history are retained."
                : "Future scheduling will pause. Work already admitted and saved history are kept."}</p>
        <div className="ns-dialog-actions"><Button autoFocus variant="outline" disabled={busy} onClick={() => setConfirm(null)}>Cancel</Button><Button disabled={busy || preview.loading || !actions?.[confirm]} onClick={() => {
          if (!actions?.[confirm]) return
          const path = { identity: id! }, intent = `${base}/${encodeURIComponent(id!)}/${confirm}`
          if (confirm === "run") void write(intent, { revision: ready.revision }, body => runProductAutomation({ path, body }), false, setRunResult)
          else if (confirm === "migrate") void write(intent, { revision: ready.revision, activate: false }, body => migrateProductAutomation({ path, body }), false)
          else void write(intent, { revision: ready.revision }, body => setProductAutomationState({ path: { ...path, action: confirm }, body }), false)
        }}>{busy ? "Saving…" : { migrate: "Migrate and keep paused", enable: "Enable automation", pause: "Pause automation", run: "Run once", retire: "Retire automation" }[confirm]}</Button></div>
      </div>}
      {runResult && <p className="ns-notice" role="status">Run status: {runResult.status}. {runResult.session_id && <a className="ns-link" href={`#${encodeURIComponent(runResult.session_id)}`} onClick={onClose}>Open session</a>}</p>}
      {ready.definition.triggers.some(trigger => trigger.event) && (caps?.webhook || caps?.test_event) && <EventTools preview={ready} busy={busy || preview.loading} version={historyVersion}
        onSetup={(provider, secret, done) => write(`${base}/${encodeURIComponent(id!)}/webhook`, { revision: ready.revision, provider, secret }, body => configureProductAutomationWebhook({ path: { identity: id! }, body }), false, done)} />}
      <HistoryRows id={id!} version={historyVersion} />
    </>}
    {mode !== "review" && models.data && (mode === "new" ? canCreate : ready && caps?.update) && <DefinitionEditor
      key={mode === "new" ? "new" : `${ready!.id}:${ready!.revision}`} existing={mode === "new" ? undefined : ready!.definition} models={models.data.models} defaultModel={models.data.default_model} eventChoices={eventChoices}
      environments={environments.data ?? []} environmentError={environments.error} environmentLoading={environments.loading} onRefreshEnvironments={environments.refresh} busy={busy}
      connections={connections.data ?? []} connectionError={connections.error} connectionLoading={connections.loading} onRefreshConnections={connections.refresh}
      onCancel={mode === "new" ? onClose : () => { setError(""); onMode("review") }}
      onSave={definition => id
        ? write(`${base}/${encodeURIComponent(id)}`, { revision: ready?.revision ?? 0, definition }, body => updateProductAutomation({ path: { identity: id }, body }), true)
        : write(base, { revision: 0, definition }, body => createProductAutomation({ body }), true)} />}
    {mode !== "review" && !models.data && models.loading && <p role="status">Loading available models…</p>}
  </DialogContent></Dialog>
}

export function UnsupportedSettings({ definition }: { definition: AutomationDefinition }) {
  const entries = [
    definition.plugins.length ? ["Connections", definition.plugins.join(", ")] : null,
    definition.repo_url || definition.github_repository_id ? ["Repository", definition.repo_url || `Repository ID ${definition.github_repository_id}`] : null,
    ["Environment", definition.environment_id],
    definition.mode !== "modal" ? ["Execution mode", definition.mode] : null,
    definition.self_management.instructions || definition.self_management.schedule ? ["Self-management", "Instructions or schedule permissions are enabled."] : null,
    Object.keys(definition.metadata).length ? ["Metadata", Object.entries(definition.metadata).map(([key, value]) => `${key}: ${value}`).join("\n")] : null,
    ["Hourly limit", definition.max_runs_per_hour === null ? "No cap" : `${definition.max_runs_per_hour} runs`],
    ["Event handling", definition.queue_events ? "Queue while another run is active" : "Allow overlapping runs"],
  ].filter((entry): entry is string[] => Boolean(entry))
  return entries.length > 0 && <div className="pa-preserved"><h3>Saved execution settings</h3><p>These settings are part of the saved definition. Current readiness determines whether they can be used.</p><dl>{entries.map(([name, value]) => <React.Fragment key={name}><dt>{name}</dt><dd>{value}</dd></React.Fragment>)}</dl></div>
}

export function TimingFields({ timing, onChange, disabled, label }: { timing: AutomationTiming; onChange: (value: AutomationTiming) => void; disabled: boolean; label: string }) {
  const id = useId(), change = (patch: Partial<AutomationTiming>) => onChange({ ...timing, ...patch })
  return <div className="pa-timing">
    <div className="ns-field"><label htmlFor={`${id}-frequency`}>Repeat</label><Choice id={`${id}-frequency`} label={`${label}: repeat`} value={timing.frequency} options={calendarFrequencies} disabled={disabled} onChange={value => change({ frequency: value as AutomationTiming["frequency"] })} /></div>
    {timing.frequency === "once" ? <div className="ns-field"><label htmlFor={`${id}-once`}>Date and time (UTC)</label><Input id={`${id}-once`} type="datetime-local" step={1} required disabled={disabled} value={onceUtcInput(timing.run_at)} onChange={event => { try { change({ run_at: event.target.value ? onceUtcInstant(event.target.value) : null }) } catch { /* Native date inputs can be incomplete during entry. */ } }} /><p className="ns-field-hint">This is an exact UTC instant. It runs once; it does not repeat or shift with daylight saving time.</p></div> : <>
      {timing.frequency === "weekly" && <div className="ns-field"><label htmlFor={`${id}-weekday`}>Day</label><Choice id={`${id}-weekday`} label={`${label}: weekday`} value={String(timing.weekday)} options={["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"].map((day, value) => ({ value: String(value), label: day }))} disabled={disabled} onChange={value => change({ weekday: Number(value) })} /></div>}
      <div className="ns-field"><label htmlFor={`${id}-time`}>{timing.frequency === "cron" ? "Cron expression" : timing.frequency === "hourly" ? "Minute within each hour" : "Time"}</label>
        {timing.frequency === "cron" ? <Input id={`${id}-time`} required maxLength={200} disabled={disabled} value={timing.cron} placeholder="0 9 * * 1-5" onChange={event => change({ cron: event.target.value })} />
          : timing.frequency === "hourly" ? <Input id={`${id}-time`} type="number" min={0} max={59} required disabled={disabled} value={Number(timing.time.slice(3))} onChange={event => change({ time: `00:${event.target.value.padStart(2, "0")}` })} />
            : <Input id={`${id}-time`} type="time" required disabled={disabled} value={timing.time} onChange={event => change({ time: event.target.value })} />}
      </div>
      <div className="ns-field"><label htmlFor={`${id}-zone`}>Timezone</label><Input id={`${id}-zone`} required disabled={disabled} maxLength={100} value={timing.timezone} placeholder="America/Los_Angeles" onChange={event => change({ timezone: event.target.value })} /><p className="ns-field-hint">Use an IANA timezone. Calendar times follow that timezone’s daylight saving rules.</p></div>
      {timing.frequency === "cron" && <p className="ns-field-hint">Five fields: minute, hour, day, month, weekday. Numbers, *, lists, ranges, and steps are supported.</p>}
    </>}
  </div>
}

function CheckField({ label, checked, disabled, onChange }: { label: string; checked: boolean; disabled: boolean; onChange: (value: boolean) => void }) {
  const id = useId()
  return <div className="pa-check"><Checkbox id={id} checked={checked} disabled={disabled} onCheckedChange={value => onChange(value === true)} /><label htmlFor={id}>{label}</label></div>
}

const eventFieldLimits: Record<string, number> = {
  session_id: 36, repository: 200, team_id: 36, assignee_id: 128, label_id: 36, label: 100, channel_id: 32,
  text_contains: 200, text_starts_with: 200, reaction: 100, action: 80, status: 100, conclusion: 60,
  branch: 200, project: 100, epic: 100, service_id: 100, urgency: 30, payload_pattern: 500,
}
export function EventFields({ event, choices, disabled, onChange }: {
  event: AutomationEvent; choices: [string, string][]; disabled: boolean; onChange: (event: AutomationEvent) => void
}) {
  const id = useId()
  const options = choices.map(([value, label]) => ({ value, label }))
  if (event.event && !options.some(option => option.value === event.event)) options.unshift({ value: event.event, label: `Saved event · ${event.event}` })
  return <div className="pa-event-fields">
    <div className="ns-field"><label htmlFor={`${id}-event`}>Event</label>{event.provider === "webhook"
      ? <Input id={`${id}-event`} required maxLength={80} value={event.event} disabled={disabled} placeholder="* matches any event" onChange={input => onChange(changeAutomationEvent(event, input.target.value))} />
      : <Choice id={`${id}-event`} label="Event type" value={event.event} options={options} disabled={disabled} onChange={value => onChange(changeAutomationEvent(event, value))} />}</div>
    <div className="pa-timing">{(automationEventFields[event.provider] ?? []).filter(([field]) => eventFieldVisible(event, field)).map(([field, label, placeholder, required]) => <div className="ns-field" key={field}>
      <label htmlFor={`${id}-${field}`}>{label}</label><Input id={`${id}-${field}`} type={field === "priority" ? "number" : "text"} min={field === "priority" ? 0 : undefined} max={field === "priority" ? 4 : undefined} step={field === "priority" ? 1 : undefined}
        required={field === "reaction" ? event.event === "reaction.added" : field === "repository" && event.provider === "github" && typeof event.repository_id === "number" ? false : required}
        maxLength={eventFieldLimits[field]} placeholder={placeholder} disabled={disabled} value={typeof event[field] === "string" || typeof event[field] === "number" ? String(event[field]) : ""}
        onChange={input => onChange(changeAutomationEventField(event, field, field === "priority" ? input.target.value === "" ? null : Number(input.target.value) : input.target.value))} />
    </div>)}</div>
    {["slack", "github", "gitlab"].includes(event.provider) && eventFieldVisible(event, "sender_type") && <div className="ns-field"><label htmlFor={`${id}-sender`}>Sender</label><Choice id={`${id}-sender`} label="Event sender" value={typeof event.sender_type === "string" ? event.sender_type : "any"} disabled={disabled} options={[{ value: "any", label: "Anyone" }, { value: "human", label: "People only" }, { value: "bot", label: "Bots only" }]} onChange={value => onChange(changeAutomationEventField(event, "sender_type", value))} /></div>}
    {event.provider === "slack" && eventFieldVisible(event, "include_thread_replies") && <CheckField label="Include thread replies" checked={event.include_thread_replies === true} disabled={disabled} onChange={value => onChange({ ...event, include_thread_replies: value })} />}
    {event.provider === "session" && <p className="ns-field-hint">Watches new human messages in shared sessions after activation. Leave the session ID blank for all eligible sessions, or use the exact original 32-character ID or native UUID. Existing messages are not replayed.</p>}
    {event.provider === "slack" && <p className="ns-field-hint">Uses the installed organization Slack app and allowed users. Add the bot to watched channels and configure the required event subscriptions and scopes.</p>}
    {event.provider === "github" && typeof event.repository_id === "number" && <p className="ns-field-hint">Saved repository ID: {event.repository_id}. Changing the repository name requires the service to resolve it again.</p>}
  </div>
}

export function DefinitionEditor({ existing, models, defaultModel, eventChoices = {}, environments = [], environmentError = "", environmentLoading = false, onRefreshEnvironments, connections = [], connectionError = "", connectionLoading = false, onRefreshConnections, busy, onCancel, onSave }: {
  existing?: AutomationDefinition; models: string[]; defaultModel: string; busy: boolean
  eventChoices?: Record<string, [string, string][]>
  environments?: AutomationEnvironment[]; environmentError?: string; environmentLoading?: boolean; onRefreshEnvironments?: () => void
  connections?: AutomationConnection[]; connectionError?: string; connectionLoading?: boolean; onRefreshConnections?: () => void
  onCancel: () => void; onSave: (definition: AutomationDefinition) => Promise<void>
}) {
  const [definition, setDefinition] = useState<AutomationDefinition>(() => existing ? structuredClone(existing) : {
    name: "", prompt: "", model: defaultModel, mode: "modal", harness: "pydantic-v1", repo_url: "", github_repository_id: null,
    environment_id: "auto", plugins: [], metadata: {}, queue_events: false, max_runs_per_hour: 50,
    self_management: { instructions: false, schedule: false },
    triggers: [{ id: crypto.randomUUID(), schedule: { ...automationDefaultSchedule(), cron: "", run_at: null } }],
  })
  const [error, setError] = useState(""), nameRef = useRef<HTMLInputElement>(null)
  const [metadata, setMetadata] = useState(() => Object.entries(existing?.metadata ?? {}).map(([key, value]) => ({ id: crypto.randomUUID(), key, value })))
  const capEdited = useRef(Boolean(existing))
  const id = useId()
  useEffect(() => { nameRef.current?.focus() }, [])
  const change = (patch: Partial<AutomationDefinition>) => setDefinition(previous => ({ ...previous, ...patch }))
  const changeTriggers = (triggers: AutomationTrigger[]) => change({ triggers, ...(!capEdited.current ? { max_runs_per_hour: triggers.some(trigger => trigger.event?.provider === "slack" && trigger.event.event === "message.posted") ? 150 : 50 } : {}) })
  const changeTrigger = (id: string, trigger: AutomationTrigger) => changeTriggers(definition.triggers.map(item => item.id === id ? trigger : item))
  const sourceOptions = [{ value: "schedule", label: "Schedule" }, ...Object.keys(eventChoices).filter(provider => automationEventFields[provider]).map(value => ({ value, label: automationSources[value] || value }))]
  const availableModels = [...new Set([definition.model, ...models])].map(value => ({ value: value || "__default__", label: value ? `${value}${models.includes(value) ? "" : " (not currently enabled)"}` : "Workspace default" }))
  const environmentOptions = [
    { value: "auto", label: "Automatic · Match repository or workspace default" },
    { value: "none", label: "Base tools · No project environment" },
    ...environments.map(environment => ({ value: environment.id, label: `${environment.name}${environment.ready ? "" : ` · ${environment.enabled ? environment.phase || "Not ready" : "Disabled"}`}${environment.is_default ? " · Workspace default" : ""}` })),
    ...(!["none", "auto"].includes(definition.environment_id) && !environments.some(environment => environment.id === definition.environment_id)
      ? [{ value: definition.environment_id, label: `Saved environment · ${definition.environment_id} (unavailable in catalog)` }] : []),
  ]
  return <form className="pa-editor" onSubmit={async event => {
    event.preventDefault(); setError("")
    try { const saved = { ...definition, metadata: automationMetadata(metadata) }; validateAutomation(saved, Date.now(), !existing); await onSave(saved) }
    catch (reason) { setError(errorLabel(reason)) }
  }}>
    <p className="ns-notice">Instructions and resulting sessions are shared with workspace members. Save only information intended for this workspace.</p>
    <div className="ns-fields">
      <div className="ns-field"><label htmlFor={`${id}-name`}>Automation name</label><Input id={`${id}-name`} ref={nameRef} value={definition.name} required minLength={2} maxLength={100} disabled={busy} onChange={event => change({ name: event.target.value })} /></div>
      <div className="ns-field"><label htmlFor={`${id}-prompt`}>Instructions</label><Textarea id={`${id}-prompt`} rows={6} required minLength={3} maxLength={500000} disabled={busy} value={definition.prompt} onChange={event => change({ prompt: event.target.value })} /></div>
      <div className="ns-field"><label htmlFor={`${id}-model`}>Model</label><Choice id={`${id}-model`} label="Automation model" value={definition.model || "__default__"} options={availableModels} disabled={busy} onChange={value => change({ model: value === "__default__" ? "" : value })} /></div>
      <div className="ns-field"><label htmlFor={`${id}-repository`}>Repository</label><Input id={`${id}-repository`} type="url" maxLength={500} placeholder="https://github.com/owner/repository" value={definition.repo_url} disabled={busy} onChange={event => setDefinition(previous => changeAutomationRepository(previous, event.target.value))} /><p className="ns-field-hint">Public repositories use a credential-free checkout. Private repositories require their selected GitHub connection. Choose a validated environment below when the workflow needs prepared dependencies.</p></div>
      <div className="ns-field"><label htmlFor={`${id}-environment`}>Execution environment</label><Choice id={`${id}-environment`} label="Execution environment" value={definition.environment_id} options={environmentOptions} disabled={busy || environmentLoading} onChange={value => change({ environment_id: value })} />
        {environmentLoading && <p className="ns-field-hint" role="status">Loading saved environments…</p>}
        {environmentError && <p className="ns-error" role="alert">{environmentError}{onRefreshEnvironments && <Button type="button" variant="outline" onClick={onRefreshEnvironments}>Retry environments</Button>}</p>}
        <p className="ns-field-hint">The service checks the selected environment and its validated build before admission. Selecting base tools removes the saved environment dependency.</p></div>
    </div>
    <fieldset className="pa-trigger-editor"><legend>Start when any trigger matches</legend>
      {definition.triggers.map((trigger, index) => {
        const source = trigger.event?.provider || "schedule", known = sourceOptions.some(option => option.value === source)
        return <section className="pa-trigger-card" key={trigger.id}>
          <header><h3>Trigger {index + 1}</h3><Button type="button" size="icon-sm" variant="ghost" aria-label={`Remove trigger ${index + 1}`} disabled={busy || definition.triggers.length === 1} onClick={() => changeTriggers(definition.triggers.filter(item => item.id !== trigger.id))}><Trash2 size={16} /></Button></header>
          <div className="ns-field"><label htmlFor={`${id}-source-${trigger.id}`}>Source</label><Choice id={`${id}-source-${trigger.id}`} label={`Trigger ${index + 1} source`} value={source} disabled={busy} options={known ? sourceOptions : [...sourceOptions, { value: source, label: `Saved source · ${automationSources[source] || source}` }]} onChange={value => {
            changeTrigger(trigger.id, value === "schedule"
              ? { id: trigger.id, schedule: { ...automationDefaultSchedule(), cron: "", run_at: null } }
              : isAutomationProvider(value, eventChoices) ? { id: trigger.id, event: { provider: value, event: eventChoices[value]?.[0]?.[0] || "*" } } : trigger)
          }} /></div>
          {trigger.schedule ? <TimingFields label={`Trigger ${index + 1}`} disabled={busy} timing={trigger.schedule} onChange={schedule => changeTrigger(trigger.id, { ...trigger, schedule })} />
            : trigger.event && known ? <EventFields event={trigger.event} choices={eventChoices[source] || []} disabled={busy} onChange={event => changeTrigger(trigger.id, { ...trigger, event })} />
              : <><p className="pa-warning">The server did not provide an editor contract for this saved source. Its original settings are retained.</p><pre>{JSON.stringify(trigger.event, null, 2)}</pre></>}
        </section>
      })}
      <Button type="button" variant="outline" disabled={busy || definition.triggers.length >= 16} onClick={() => changeTriggers([...definition.triggers, { id: crypto.randomUUID(), schedule: { ...automationDefaultSchedule(), cron: "", run_at: null } }])}><Plus size={16} />Add trigger</Button>
    </fieldset>
    <section className="pa-editor-section"><h3>Connections and tools</h3><p className="ns-field-hint">Choose which connected apps this workflow may use. The service rechecks account permissions for shared automation sessions before activation.</p>
      {connectionLoading && <p role="status" className="ns-muted">Loading connected apps…</p>}
      {connectionError && <p className="ns-error" role="alert">{connectionError}{onRefreshConnections && <Button type="button" variant="outline" onClick={onRefreshConnections}>Retry connections</Button>}</p>}
      {(["linear", "github", "slack", "notion"] as const).map(provider => {
        const account = connections.find(connection => connection.id === provider), selected = definition.plugins.includes(provider)
        return <div className="pa-connection" key={provider}><CheckField label={`${automationSources[provider] || (provider === "notion" ? "Notion" : provider)} · ${account ? !account.connected ? "Not connected" : !account.enabled ? "Disabled" : account.read_only ? "Read only" : "Connected" : "Availability unavailable"}`}
          checked={selected} disabled={busy || (!selected && (connectionLoading || !account?.connected || !account.enabled))} onChange={checked => change({ plugins: checked ? [...definition.plugins, provider] : definition.plugins.filter(value => value !== provider) })} />
          {Boolean(account?.tools?.length) && <details><summary>{account!.tools!.length} tools</summary><ul>{account!.tools!.map((tool, index) => <li key={`${tool.name}:${index}`}>{tool.name && <code>{tool.name} </code>}{tool.description}</li>)}</ul></details>}
        </div>
      })}
    </section>
    <section className="pa-editor-section"><h3>Self-management</h3><p className="ns-field-hint">Allow this automation and its workers to change only the selected parts of this workflow. Schedule changes may affect frequency and cost within your invocation limit.</p>
      <CheckField label="May revise its instructions" disabled={busy} checked={definition.self_management.instructions} onChange={instructions => change({ self_management: { ...definition.self_management, instructions } })} />
      <CheckField label="May adjust existing schedules" disabled={busy} checked={definition.self_management.schedule} onChange={schedule => change({ self_management: { ...definition.self_management, schedule } })} />
    </section>
    <section className="pa-editor-section"><h3>Metadata</h3><p className="ns-field-hint">Descriptive key-value pairs for organizing this workflow. Metadata does not configure delivery or execute actions.</p>
      {metadata.map((entry, index) => <div className="pa-metadata" key={entry.id}>
        <div className="ns-field"><label htmlFor={`${id}-key-${entry.id}`}>Key {index + 1}</label><Input id={`${id}-key-${entry.id}`} required value={entry.key} disabled={busy} onChange={event => setMetadata(rows => rows.map(row => row.id === entry.id ? { ...row, key: event.target.value } : row))} /></div>
        <div className="ns-field"><label htmlFor={`${id}-value-${entry.id}`}>Value {index + 1}</label><Textarea id={`${id}-value-${entry.id}`} rows={2} value={entry.value} disabled={busy} onChange={event => setMetadata(rows => rows.map(row => row.id === entry.id ? { ...row, value: event.target.value } : row))} /></div>
        <Button type="button" variant="ghost" size="icon-sm" aria-label={`Remove metadata ${index + 1}`} disabled={busy} onClick={() => setMetadata(rows => rows.filter(row => row.id !== entry.id))}><Trash2 size={16} /></Button>
      </div>)}
      <Button type="button" variant="outline" disabled={busy} onClick={() => setMetadata(rows => [...rows, { id: crypto.randomUUID(), key: "", value: "" }])}><Plus size={16} />Add metadata</Button>
    </section>
    <section className="pa-editor-section"><h3>Limits and queueing</h3>
      <div className="ns-field"><label htmlFor={`${id}-limit`}>Maximum runs per hour</label><Input id={`${id}-limit`} type="number" min={1} step={1} disabled={busy} value={definition.max_runs_per_hour ?? ""} placeholder="No hourly cap" onChange={event => { capEdited.current = true; change({ max_runs_per_hour: event.target.value === "" ? null : Number(event.target.value) }) }} /><p className="ns-field-hint">Shared across all triggers. Clear for no hourly cap. Defaults to 50, or 150 for Slack message watching.</p></div>
      <CheckField label="Queue overlapping event runs" checked={definition.queue_events} disabled={busy} onChange={queue_events => change({ queue_events })} /><p className="ns-field-hint">Events wait for the previous run when queueing is selected. Scheduled runs remain independent; workspace capacity limits still apply.</p>
    </section>
    {definition.mode !== "modal" && <p className="pa-warning">This saved execution mode ({definition.mode}) is unsupported by the native service. Its value is retained; migration remains blocked.</p>}
    {!models.length && <p className="pa-warning">No models are currently available. Activation remains unavailable until model access is configured.</p>}
    {error && <p className="ns-error" role="alert">{error}</p>}
    <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={busy} onClick={onCancel}>{existing ? <><ChevronLeft size={16} />Back to review</> : "Cancel"}</Button><Button type="submit" disabled={busy || (!existing && !models.length)}>{busy ? "Saving…" : "Save paused"}</Button></div>
  </form>
}
