import React, { useCallback, useEffect, useId, useRef, useState } from "react"
import { Activity, ArrowLeft, ArrowRight, BookOpen, CalendarClock, ChevronRight, Download, KeyRound, LayoutGrid, Link2, Menu, Pencil, Plus, RefreshCw, Search, Shield, Sparkles, Trash2, Users, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, ApiError } from "./api"
import logo from "../../app/static/moyai-logo.png"
import "./settings.css"

type SettingsUser = { id: string; name: string; role: string }
type SettingsPageProps = { user: SettingsUser; page: string; onNavigate: (page: string) => void; onBack: () => void }
type Capabilities = { create?: boolean; update?: boolean; delete?: boolean; reason?: string; runtime_available?: boolean; shared?: boolean; run?: boolean; agent_access?: boolean }
type Collection = { capabilities?: Capabilities; reason?: string }
type Skill = { id: string; name: string; description: string; content: string; scope: string; enabled: boolean; editable: boolean; updated_at?: string }
type Secret = { id: string; name: string; description: string; scope: string; editable?: boolean; updated_at?: string }
type Memory = { namespace: string; path: string; content: string; version?: string; truncated?: boolean }
type Automation = { id: string; name: string; prompt: string; session_id: string; model: string; interval_minutes: number; paused: boolean; state: string; updated_at?: string }
type Account = { id: string; username: string; display_name: string; role: string; enabled: boolean }
type Connection = { id: string; name: string; kind: string; owner_id: string; shared: boolean; read_only: boolean; enabled: boolean; policy?: { allowed_tools?: string[] | null; require_approval?: boolean } }
type Option = { value: string; label: string }
type Resource<T> = { data: T | undefined; loading: boolean; error: string; refresh: () => void }
type FormValues = Record<string, string | number | boolean>
type Field = { name: string; label: string; type?: "text" | "password" | "textarea" | "select" | "checkbox" | "number"; required?: boolean; hint?: string; options?: Option[]; disabled?: boolean; min?: number; max?: number; minLength?: number; maxLength?: number; pattern?: string }

const destinations = [
  { id: "skills", title: "Skills", description: "Save instructions for yourself or your team.", group: "Workflows", Icon: Sparkles },
  { id: "automations", title: "Automations", description: "Schedule recurring work and review past runs.", group: "Workflows", Icon: CalendarClock },
  { id: "memory", title: "Memory", description: "Review the context available to your future sessions.", group: "Workflows", Icon: BookOpen },
  { id: "connections", title: "Connections", description: "Choose the accounts Moyai can use in your sessions.", group: "Integrations", Icon: Link2 },
  { id: "secrets", title: "Secrets", description: "Manage personal and shared service credentials.", group: "Integrations", Icon: KeyRound },
  { id: "users", title: "Users", description: "Manage workspace members, roles, and access.", group: "Administration", Icon: Users, admin: true },
  { id: "performance", title: "Performance", description: "Review recorded turn timings across the workspace.", group: "Administration", Icon: Activity, admin: true },
  { id: "usage", title: "Spend & usage", description: "Review recorded model usage and cost coverage.", group: "Administration", Icon: Activity, admin: true },
]
const groups = ["Workflows", "Integrations", "Administration"]
const scopeOptions = [{ value: "all", label: "All scopes" }, { value: "personal", label: "Personal" }, { value: "shared", label: "Shared" }, { value: "bundled", label: "Bundled" }]
const scopes = (user: SettingsUser, capabilities?: Capabilities) => scopeOptions.filter(option => option.value === "personal" || (user.role === "admin" && capabilities?.shared === true && option.value === "shared"))
const contains = (query: string, ...values: (string | undefined)[]) => values.join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
const count = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "Unavailable"
const dateTime = (value?: string) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not recorded"
const errorText = (error: unknown) => error instanceof ApiError && error.status === 404
  ? "This settings endpoint is not available on this server."
  : error instanceof Error ? error.message : "The request could not be completed."

function useResource<T>(path: string): Resource<T> {
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<{ path: string; data?: T; loading: boolean; error: string }>({ path, loading: true, error: "" })
  useEffect(() => {
    const controller = new AbortController()
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true, error: "" }))
    void api<T>(path, "GET", undefined, controller.signal).then(
      data => { if (!controller.signal.aborted) setState({ path, data, loading: false, error: "" }) },
      error => { if (!controller.signal.aborted) setState({ path, loading: false, error: errorText(error) }) },
    )
    return () => controller.abort()
  }, [path, revision])
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  return { data: state.path === path ? state.data : undefined, loading: state.path !== path || state.loading, error: state.path === path ? state.error : "", refresh }
}

function useMutation() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState("")
  const alive = useRef(true)
  const pending = useRef(false)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const run = async (action: () => Promise<unknown>, done: () => void) => {
    if (pending.current) return
    pending.current = true; setBusy(true); setError("")
    try { await action(); if (alive.current) done() }
    catch (error) { if (alive.current) setError(errorText(error)) }
    finally { pending.current = false; if (alive.current) setBusy(false) }
  }
  return { busy, error, run }
}

function Choice({ label, value, onChange, options, disabled, id }: { label: string; value: string; onChange: (value: string) => void; options: Option[]; disabled?: boolean; id?: string }) {
  return <Select value={value} onValueChange={onChange} disabled={disabled}>
    <SelectTrigger id={id} aria-label={label} title={options.find(option => option.value === value)?.label}><SelectValue placeholder={label} /></SelectTrigger>
    <SelectContent className="native-settings-select">{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent>
  </Select>
}

function LoadState<T>({ resource, children }: { resource: Resource<T>; children: (data: T) => React.ReactNode }) {
  if (resource.error) return <div className="ns-empty" role="alert"><h2>Unable to load this page</h2><p>{resource.error}</p><Button variant="outline" onClick={resource.refresh}><RefreshCw />Try again</Button></div>
  if (!resource.data) return <p className="ns-muted" role="status">Loading settings…</p>
  return <div aria-busy={resource.loading}>{children(resource.data)}</div>
}

function Toolbar({ search, setSearch, count: total, resource, filter, action }: {
  search: string; setSearch: (value: string) => void; count?: number; resource: { loading: boolean; refresh: () => void }
  filter?: React.ReactNode; action?: React.ReactNode
}) {
  return <div className="ns-toolbar">
    <div className="ns-search"><Search size={16} aria-hidden="true" /><Input type="search" aria-label="Search this list" placeholder="Search…" value={search} onChange={event => setSearch(event.target.value)} />
      {search && <Button variant="ghost" size="icon-sm" aria-label="Clear search" onClick={() => setSearch("")}><X /></Button>}</div>
    {filter}{total !== undefined && <span className="ns-count">{count(total)}</span>}
    <Button type="button" variant="outline" disabled={resource.loading} onClick={resource.refresh} aria-label="Refresh settings"><RefreshCw size={16} /><span>Refresh</span></Button>
    {action}
  </div>
}

function Empty({ filtered, noun, onClear }: { filtered: boolean; noun: string; onClear: () => void }) {
  return <div className="ns-empty"><h2>{filtered ? `No matching ${noun}` : `No ${noun} yet`}</h2>
    <p>{filtered ? "Try another search or clear the filters." : `Saved ${noun} will appear here.`}</p>
    {filtered && <Button variant="outline" onClick={onClear}>Clear filters</Button>}
  </div>
}

function CapabilityNote({ capabilities, reason }: Collection) {
  return (reason || capabilities?.reason) ? <p className="ns-notice" role="status">{reason || capabilities?.reason}</p>
    : !capabilities ? <p className="ns-notice">Editing is not enabled by this server.</p> : null
}

function RecordTable({ label, headings, children }: { label: string; headings: string[]; children: React.ReactNode }) {
  return <div className="ns-table" role="region" aria-label={label} tabIndex={0}><Table>
    <TableHeader><TableRow>{headings.map(heading => <TableHead key={heading} scope="col">{heading}</TableHead>)}</TableRow></TableHeader>
    <TableBody>{children}</TableBody>
  </Table></div>
}

function Editor({ title, description, fields, initial, onSave, onClose, submitLabel = "Save" }: {
  title: string; description: string; fields: Field[] | ((values: FormValues) => Field[]); initial: FormValues
  onSave: (values: FormValues) => Promise<unknown>; onClose: () => void; submitLabel?: string
}) {
  const [values, setValues] = useState(initial)
  const mutation = useMutation()
  const id = useId()
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy) onClose() }}>
    <DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
      <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
      <form onSubmit={event => { event.preventDefault(); void mutation.run(() => onSave(values), onClose) }}>
        <div className="ns-fields">{(typeof fields === "function" ? fields(values) : fields).map((field, index) => {
          const fieldId = `${id}-${field.name}`, hintId = `${fieldId}-hint`
          const change = (value: string | number | boolean) => setValues(previous => ({ ...previous, [field.name]: value }))
          return <div className={`ns-field ${field.type === "checkbox" ? "ns-check" : ""}`} key={field.name}>
            {field.type === "checkbox" ? <><Checkbox id={fieldId} checked={values[field.name] === true} disabled={mutation.busy || field.disabled} onCheckedChange={value => change(value === true)} /><label htmlFor={fieldId}>{field.label}</label></>
              : <><label htmlFor={fieldId}>{field.label}</label>
                {field.type === "textarea" ? <Textarea id={fieldId} rows={8} value={String(values[field.name] ?? "")} required={field.required} maxLength={field.maxLength} disabled={mutation.busy || field.disabled} aria-describedby={field.hint ? hintId : undefined} onChange={event => change(event.target.value)} />
                  : field.type === "select" ? <Choice id={fieldId} label={field.label} value={String(values[field.name] ?? "")} options={field.options ?? []} disabled={mutation.busy || field.disabled} onChange={change} />
                    : <Input id={fieldId} autoFocus={index === 0} type={field.type ?? "text"} value={String(values[field.name] ?? "")} required={field.required} min={field.min} max={field.max} minLength={field.minLength} maxLength={field.maxLength} pattern={field.pattern} disabled={mutation.busy || field.disabled} autoComplete={field.type === "password" ? "new-password" : "off"} aria-describedby={field.hint ? hintId : undefined} onChange={event => change(field.type === "number" && event.target.value !== "" ? Number(event.target.value) : event.target.value)} />}</>}
            {field.hint && <p id={hintId} className="ns-field-hint">{field.hint}</p>}
          </div>
        })}</div>
        {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
        <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={mutation.busy} onClick={onClose}>Cancel</Button><Button type="submit" disabled={mutation.busy}>{mutation.busy ? "Saving…" : submitLabel}</Button></div>
      </form>
    </DialogContent>
  </Dialog>
}

function Confirm({ title, description, label, onConfirm, onClose }: { title: string; description: string; label: string; onConfirm: () => Promise<unknown>; onClose: () => void }) {
  const mutation = useMutation()
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
    <DialogHeader><DialogTitle>{title}</DialogTitle><DialogDescription>{description}</DialogDescription></DialogHeader>
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
    <div className="ns-dialog-actions"><Button autoFocus variant="outline" disabled={mutation.busy} onClick={onClose}>Cancel</Button><Button variant="destructive" disabled={mutation.busy} onClick={() => void mutation.run(onConfirm, onClose)}>{mutation.busy ? "Saving…" : label}</Button></div>
  </DialogContent></Dialog>
}

function SkillsPage({ user }: { user: SettingsUser }) {
  const resource = useResource<Collection & { skills: Skill[] }>("/settings/skills")
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<Skill | "new" | null>(null), [remove, setRemove] = useState<Skill | null>(null)
  const clear = () => { setSearch(""); setScope("all") }
  return <LoadState resource={resource}>{data => {
    const rows = data.skills.filter(row => contains(search, row.name, row.description, row.content) && (scope === "all" || row.scope === scope))
    return <><CapabilityNote {...data} />
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Skill scope" value={scope} onChange={setScope} options={scopeOptions} />}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />New skill</Button>} />
      {rows.length ? <div className="ns-records">{rows.map(row => <article className="ns-record" key={row.id}>
        <div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.scope}</span>{!row.enabled && <span className="ns-badge">Disabled</span>}</div><p>{row.description}</p>
          <details><summary>View instructions</summary><pre>{row.content}</pre></details><small>Updated {dateTime(row.updated_at)}</small></div>
        <div className="ns-row-actions">{row.editable && data.capabilities?.update && <Button variant="outline" size="sm" onClick={() => setEditor(row)} aria-label={`Edit ${row.name}`}><Pencil />Edit</Button>}
          {row.editable && data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
      </article>)}</div> : <Empty filtered={Boolean(search) || scope !== "all"} noun="skills" onClear={clear} />}
      {editor && <Editor title={editor === "new" ? "New skill" : "Edit skill"} description="Instructions are available to the selected scope. Bundled skills are managed by the server." fields={[
        { name: "name", label: "Name", required: true, maxLength: 64, pattern: "[a-z0-9]+(?:-[a-z0-9]+)*", hint: "Lowercase letters and numbers, separated by hyphens." }, { name: "description", label: "Description", required: true, maxLength: 1024 },
        { name: "scope", label: "Scope", type: "select", options: scopes(user, data.capabilities) }, { name: "content", label: "Instructions", type: "textarea", required: true, maxLength: 65536 },
        { name: "enabled", label: "Enable this skill", type: "checkbox" },
      ]} initial={editor === "new" ? { name: "", description: "", scope: "personal", content: "", enabled: true } : { name: editor.name, description: editor.description, scope: editor.scope, content: editor.content, enabled: editor.enabled }}
        onSave={async values => { await api(`/settings/skills${editor === "new" ? "" : `/${encodeURIComponent(editor.id)}`}`, editor === "new" ? "POST" : "PATCH", values); resource.refresh() }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="This removes the saved skill. Existing conversations are kept." label="Delete skill" onConfirm={async () => { await api(`/settings/skills/${encodeURIComponent(remove.id)}`, "DELETE"); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function SecretsPage({ user }: { user: SettingsUser }) {
  const resource = useResource<Collection & { secrets: Secret[] }>("/settings/secrets")
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<Secret | "new" | null>(null), [remove, setRemove] = useState<Secret | null>(null)
  return <LoadState resource={resource}>{data => {
    const rows = data.secrets.filter(row => contains(search, row.name, row.description) && (scope === "all" || row.scope === scope))
    const editable = (row: Secret) => row.editable !== false && (row.scope === "personal" || user.role === "admin")
    return <><p className="ns-notice"><Shield size={16} />Secret values are never returned to this page. Enter a new value only to replace one.</p><CapabilityNote {...data} />
      {data.capabilities?.agent_access === false && <p className="ns-muted">Saved secrets are not automatically passed to the agent. Use Connections for supported services.</p>}
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Secret scope" value={scope} onChange={setScope} options={scopeOptions.filter(item => item.value !== "bundled")} />}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add secret</Button>} />
      {rows.length ? <RecordTable label="Saved secrets" headings={["Name", "Scope", "Value", "Updated", "Actions"]}>{rows.map(row => <TableRow key={row.id}>
        <TableCell><strong>{row.name}</strong><small>{row.description}</small></TableCell><TableCell>{row.scope}</TableCell><TableCell><span aria-label="Value hidden">••••••••</span></TableCell><TableCell>{dateTime(row.updated_at)}</TableCell>
        <TableCell><div className="ns-row-actions">{editable(row) && data.capabilities?.update && <Button variant="outline" size="sm" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.name}</span></Button>}
          {editable(row) && data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div></TableCell>
      </TableRow>)}</RecordTable> : <Empty filtered={Boolean(search) || scope !== "all"} noun="secrets" onClear={() => { setSearch(""); setScope("all") }} />}
      {editor && <Editor title={editor === "new" ? "Add secret" : "Edit secret"} description="The value is saved securely and stays hidden after saving." initial={editor === "new" ? { name: "", description: "", scope: "personal", value: "" } : { name: editor.name, description: editor.description, scope: editor.scope, value: "" }} fields={[
        { name: "name", label: "Name", required: true, maxLength: 100, pattern: "[A-Za-z][A-Za-z0-9_.\\-]*", hint: "Start with a letter; use letters, numbers, underscores, dots, or hyphens." }, { name: "description", label: "Description", maxLength: 1000 },
        { name: "scope", label: "Scope", type: "select", options: scopes(user, data.capabilities) },
        { name: "value", label: editor === "new" ? "Secret value" : "Replacement value", type: "password", required: editor === "new", maxLength: 65536, hint: editor === "new" ? "This value will not be displayed again." : "Leave blank to keep the current value." },
      ]} onSave={async values => { const body = { ...values }; if (editor !== "new" && !body.value) delete body.value; await api(`/settings/secrets${editor === "new" ? "" : `/${encodeURIComponent(editor.id)}`}`, editor === "new" ? "POST" : "PATCH", body); resource.refresh() }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="Future work that needs this credential may fail until a replacement is saved." label="Delete secret" onConfirm={async () => { await api(`/settings/secrets/${encodeURIComponent(remove.id)}`, "DELETE"); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function MemoryPage() {
  const [namespace, setNamespace] = useState("")
  const resource = useResource<Collection & { namespaces: { id: string; label: string }[]; default_namespace: string; namespace: string; memories: Memory[]; truncated?: boolean }>(`/settings/memory${namespace ? `?${new URLSearchParams({ namespace })}` : ""}`)
  const [options, setOptions] = useState<Option[]>([])
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<Memory | "new" | null>(null), [remove, setRemove] = useState<Memory | null>(null)
  useEffect(() => { if (resource.data) setOptions(resource.data.namespaces.map(item => ({ value: item.id, label: item.label }))) }, [resource.data])
  const memoryPath = (row: { namespace: string; path: string }, version?: string) => `/settings/memory?${new URLSearchParams({ namespace: row.namespace, path: row.path, ...(version ? { version } : {}) })}`
  return <>
    <p className="ns-notice"><Shield size={16} />Only memory namespaces available to your own sessions are listed. Other users’ private memory is not requested.</p>
    {options.length > 0 && <div className="ns-scope-picker"><label htmlFor="memory-namespace">Memory namespace</label><Choice id="memory-namespace" label="Memory namespace" value={namespace || resource.data?.namespace || resource.data?.default_namespace || options[0].value} onChange={value => { setNamespace(value); setEditor(null); setRemove(null) }} options={options} /></div>}
    <LoadState resource={resource}>{data => {
      const rows = data.memories.filter(row => contains(search, row.path, row.content))
      return <><CapabilityNote {...data} />{data.truncated && <p className="ns-notice">Showing the first {count(data.memories.length)} memory files in this namespace. Additional files are not included in this list.</p>}<Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add memory</Button>} />
        {rows.length ? <div className="ns-records">{rows.map(row => <article key={`${row.namespace}/${row.path}`} className="ns-record">
          <div className="ns-record-copy"><h2>{row.path}</h2><details><summary>View memory</summary><pre>{row.content}</pre></details>{row.truncated && <p className="ns-muted">This preview is truncated. Editing is unavailable to avoid overwriting unseen content.</p>}</div>
          <div className="ns-row-actions">{data.capabilities?.update && !row.truncated && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.path}</span></Button>}
            {data.capabilities?.delete && <Button size="icon-sm" variant="ghost" aria-label={`Delete ${row.path}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
        </article>)}</div> : <Empty filtered={Boolean(search)} noun="memories" onClear={() => setSearch("")} />}
        {editor && <Editor title={editor === "new" ? "Add memory" : "Edit memory"} description="Changes apply to the selected namespace. A newer saved version will not be overwritten." initial={{ path: editor === "new" ? "" : editor.path, content: editor === "new" ? "" : editor.content }} fields={[
          { name: "path", label: "Path", required: true, disabled: editor !== "new", pattern: "(?!.*\\.\\.)[A-Za-z0-9_][A-Za-z0-9_.\\-]{0,199}\\.md", maxLength: 203, hint: "Use a Markdown filename such as preferences.md, without directory segments." },
          { name: "content", label: "Memory content", type: "textarea", required: true, maxLength: 65536 },
        ]} onSave={async values => { await api(memoryPath({ namespace: editor === "new" ? data.namespace : editor.namespace, path: String(values.path) }), "PUT", { content: values.content, version: editor === "new" ? null : editor.version }); resource.refresh() }} onClose={() => setEditor(null)} />}
        {remove && <Confirm title={`Delete ${remove.path}?`} description="This removes the saved memory from this namespace. Existing conversations are kept." label="Delete memory" onConfirm={async () => { await api(memoryPath(remove, remove.version), "DELETE"); resource.refresh() }} onClose={() => setRemove(null)} />}
      </>
    }}</LoadState>
  </>
}

function ConnectionsPage({ user }: { user: SettingsUser }) {
  const resource = useResource<Connection[]>("/connections")
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [editor, setEditor] = useState<Connection | "new" | null>(null), [disable, setDisable] = useState<Connection | null>(null)
  const services = [{ value: "github", label: "GitHub" }, { value: "slack", label: "Slack" }, { value: "linear", label: "Linear" }, { value: "notion", label: "Notion" }]
  const connectionBody = (row: Connection) => ({ name: row.name, kind: row.kind, credential: "", shared: row.shared, read_only: row.read_only, enabled: row.enabled, policy: row.policy ?? { require_approval: true } })
  return <LoadState resource={resource}>{data => {
    const rows = data.filter(row => contains(search, row.name, row.kind) && (filter === "all" || row.kind === filter))
    return <><p className="ns-muted">Select connections for each message in Context & connections. Credentials stay hidden after saving.</p>
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Service" value={filter} onChange={setFilter} options={[{ value: "all", label: "All services" }, ...services]} />}
        action={<Button onClick={() => setEditor("new")}><Plus />Add connection</Button>} />
      {rows.length ? <div className="ns-records">{rows.map(row => <article className="ns-record" key={row.id}><Link2 className="ns-record-icon" aria-hidden="true" />
        <div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.shared ? "Shared" : "Personal"}</span></div>
          <p>{services.find(service => service.value === row.kind)?.label ?? row.kind} · {row.enabled ? row.read_only ? "Read only" : row.policy?.require_approval === false ? "Can make changes" : "Changes require approval" : "Disabled"}</p>
          <small>{row.owner_id === user.id ? row.shared && user.role !== "admin" ? "Administrator access is required to edit this shared connection." : "Your connection" : "Managed by another workspace member"}</small></div>
        {row.owner_id === user.id && (!row.shared || user.role === "admin") && <div className="ns-row-actions"><Button variant="outline" size="sm" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.name}</span></Button>{row.enabled && <Button variant="ghost" size="sm" onClick={() => setDisable(row)}>Disable<span className="visually-hidden"> {row.name}</span></Button>}</div>}
      </article>)}</div> : <Empty filtered={Boolean(search) || filter !== "all"} noun="connections" onClear={() => { setSearch(""); setFilter("all") }} />}
      {editor && <Editor title={editor === "new" ? "Add connection" : "Edit connection"} description="Use an access token for the selected service. Interactive OAuth is not supported by this connection form." initial={editor === "new" ? { name: "", kind: "github", credential: "", shared: false, read_only: true, enabled: true } : { name: editor.name, kind: editor.kind, credential: "", shared: editor.shared, read_only: editor.read_only, enabled: editor.enabled }} fields={values => [
        { name: "name", label: "Account name", required: true, maxLength: 150 }, { name: "kind", label: "Service", type: "select", options: services, disabled: editor !== "new" },
        { name: "credential", label: editor === "new" ? "Access token" : "Replacement token", type: "password", required: editor === "new", hint: values.kind === "slack" ? "Use a Slack user token, not a bot token." : values.kind === "notion" ? "Use a Notion OAuth access token, not an integration token." : editor === "new" ? "Token permissions determine which resources this account can reach." : "Leave blank to keep the current token." },
        { name: "read_only", label: "Read only", type: "checkbox" }, { name: "enabled", label: "Enable this connection", type: "checkbox" },
        ...(user.role === "admin" ? [{ name: "shared", label: "Share with workspace members", type: "checkbox" as const }] : []),
      ]} onSave={async values => { await api(`/connections${editor === "new" ? "" : `/${encodeURIComponent(editor.id)}`}`, editor === "new" ? "POST" : "PUT", { ...values, policy: editor === "new" ? { require_approval: true } : editor.policy ?? { require_approval: true } }); resource.refresh() }} onClose={() => setEditor(null)} />}
      {disable && <Confirm title={`Disable ${disable.name}?`} description="New work cannot use this connection until you enable it again. The saved credential is kept." label="Disable connection" onConfirm={async () => { await api(`/connections/${encodeURIComponent(disable.id)}`, "PUT", { ...connectionBody(disable), enabled: false }); resource.refresh() }} onClose={() => setDisable(null)} />}
    </>
  }}</LoadState>
}

function UsersPage({ user }: { user: SettingsUser }) {
  const resource = useResource<Collection & { users: Account[] }>("/settings/users")
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [editor, setEditor] = useState<Account | "new" | null>(null), [remove, setRemove] = useState<Account | null>(null)
  return <LoadState resource={resource}>{data => {
    const rows = data.users.filter(row => contains(search, row.username, row.display_name) && (filter === "all" || (filter === "disabled" ? !row.enabled : row.role === filter)))
    return <><CapabilityNote {...data} /><Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
      filter={<Choice label="User role or access" value={filter} onChange={setFilter} options={[{ value: "all", label: "All members" }, { value: "admin", label: "Administrators" }, { value: "member", label: "Members" }, { value: "disabled", label: "Disabled" }]} />}
      action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add user</Button>} />
      {rows.length ? <RecordTable label="Workspace users" headings={["User", "Role", "Access", "Actions"]}>{rows.map(row => <TableRow key={row.id}>
        <TableCell><strong>{row.display_name || row.username}{row.id === user.id ? " (you)" : ""}</strong><small>{row.username}</small></TableCell><TableCell>{row.role === "admin" ? "Administrator" : "Member"}</TableCell><TableCell><span className={`ns-badge ${row.enabled ? "ns-success" : ""}`}>{row.enabled ? "Enabled" : "Disabled"}</span></TableCell>
        <TableCell><div className="ns-row-actions">{data.capabilities?.update && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.username}</span></Button>}
          {data.capabilities?.delete && row.enabled && row.id !== user.id && <Button size="sm" variant="ghost" onClick={() => setRemove(row)}>Disable<span className="visually-hidden"> {row.username}</span></Button>}</div></TableCell>
      </TableRow>)}</RecordTable> : <Empty filtered={Boolean(search) || filter !== "all"} noun="users" onClear={() => { setSearch(""); setFilter("all") }} />}
      {editor && <Editor title={editor === "new" ? "Add user" : "Edit user"} description={editor === "new" ? "Share new credentials through an approved channel." : "Saving changes revokes existing sign-ins and stops this user’s pending or running work. Saved history is retained."} initial={editor === "new" ? { username: "", display_name: "", password: "", role: "member", enabled: true } : { username: editor.username, display_name: editor.display_name, password: "", role: editor.role, enabled: editor.enabled }} fields={[
        { name: "username", label: "Username", required: true, disabled: editor !== "new", maxLength: 200, pattern: "[A-Za-z0-9][A-Za-z0-9_.@+\\-]*" }, { name: "display_name", label: "Display name", required: true, maxLength: 200 },
        { name: "password", label: editor === "new" ? "Password" : "New password", type: "password", required: editor === "new", minLength: 12, maxLength: 4096, hint: editor === "new" ? "Use at least 12 characters and a unique password." : "Leave blank to keep the existing password; replacements need at least 12 characters." },
        { name: "role", label: "Role", type: "select", options: [{ value: "member", label: "Member" }, { value: "admin", label: "Administrator" }], disabled: editor !== "new" && editor.id === user.id },
        { name: "enabled", label: "Enable workspace access", type: "checkbox", disabled: editor !== "new" && editor.id === user.id },
      ]} onSave={async values => {
        if (editor === "new") await api("/settings/users", "POST", values)
        else {
          const original: FormValues = { display_name: editor.display_name, role: editor.role, enabled: editor.enabled }
          const body = Object.fromEntries(Object.entries(values).filter(([name, value]) => name !== "username" && (name === "password" ? Boolean(value) : value !== original[name])))
          if (!Object.keys(body).length) return
          await api(`/settings/users/${encodeURIComponent(editor.id)}`, "PATCH", body)
        }
        resource.refresh()
      }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Disable ${remove.display_name || remove.username}?`} description="This revokes workspace access and existing sign-ins, and stops this user’s pending or running work. Saved history is retained." label="Disable user" onConfirm={async () => { await api(`/settings/users/${encodeURIComponent(remove.id)}`, "DELETE"); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function AutomationEditor({ user, onClose, refresh }: { user: SettingsUser; onClose: () => void; refresh: () => void }) {
  const sessions = useResource<{ id: string; title: string; owner_id: string; archived: boolean; deleted?: boolean; surface: string; visibility: string }[]>("/sessions")
  const models = useResource<{ models: string[]; default_model: string }>("/me")
  const connections = useResource<Connection[]>("/connections")
  if (sessions.error || models.error || connections.error || !sessions.data || !models.data || !connections.data) return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog"><DialogHeader><DialogTitle>New automation</DialogTitle><DialogDescription>Choose an active session and a model available to your account.</DialogDescription></DialogHeader>
    {sessions.error || models.error || connections.error ? <><p className="ns-error" role="alert">{sessions.error || models.error || connections.error}</p><Button variant="outline" onClick={() => { sessions.refresh(); models.refresh(); connections.refresh() }}>Try again</Button></> : <p role="status">Loading sessions and models…</p>}
  </DialogContent></Dialog>
  const eligible = sessions.data.filter(row => row.owner_id === user.id && !row.archived && !row.deleted && row.surface === "web" && row.visibility === "private")
  if (!eligible.length || !models.data.models.length) return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog"><DialogHeader><DialogTitle>New automation</DialogTitle><DialogDescription>{!eligible.length ? "Create an active web session you own before scheduling work." : "No models are available to this account. Ask an administrator to configure model access."}</DialogDescription></DialogHeader><Button variant="outline" onClick={onClose}>Close</Button></DialogContent></Dialog>
  const availableConnections = connections.data.filter(row => row.enabled)
  const modelOptions = models.data.models.map(value => ({ value, label: value }))
  return <Editor title="New automation" description="Recurring work runs in the selected session using the chosen model and accounts. Review the prompt and interval before saving." submitLabel="Create automation"
    initial={{ name: "", prompt: "", session_id: eligible[0].id, model: modelOptions.some(row => row.value === models.data?.default_model) ? models.data.default_model : modelOptions[0].value, interval_minutes: 1440 }} fields={values => [
      { name: "name", label: "Name", required: true, maxLength: 150 }, { name: "prompt", label: "Instructions", type: "textarea", required: true, maxLength: 100000 },
      { name: "session_id", label: "Session", type: "select", options: eligible.map(row => ({ value: row.id, label: row.title })) },
      { name: "model", label: "Model", type: "select", options: modelOptions }, { name: "interval_minutes", label: "Repeat every (minutes)", type: "number", required: true, min: 5, max: 525600 },
      ...availableConnections.filter(row => eligible.find(session => session.id === values.session_id)?.visibility === "private" || row.shared).map(row => ({ name: `connection:${row.id}`, label: `Use ${row.name}${row.read_only ? " (read only)" : ""}`, type: "checkbox" as const })),
    ]} onSave={async values => {
      const selected = eligible.find(row => row.id === values.session_id)
      const selectedConnections = availableConnections.filter(row => values[`connection:${row.id}`] === true && (selected?.visibility === "private" || row.shared)).map(row => row.id)
      await api("/settings/automations", "POST", { name: values.name, prompt: values.prompt, session_id: values.session_id, model: values.model, interval_minutes: values.interval_minutes, connections: selectedConnections }); refresh()
    }} onClose={onClose} />
}

function AutomationLogs({ automation, onClose }: { automation: Automation; onClose: () => void }) {
  const resource = useResource<{ logs: { id: string; turn_id: string | null; status: string; created_at: string }[] }>(`/settings/automations/${encodeURIComponent(automation.id)}/logs`)
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
    <DialogHeader><DialogTitle>{automation.name}: run history</DialogTitle><DialogDescription>Recorded admissions and their status. Open the session to review a turn’s output.</DialogDescription></DialogHeader>
    <LoadState resource={resource}>{data => data.logs.length ? <RecordTable label="Automation run history" headings={["Created", "Status", "Turn"]}>{data.logs.map(row => <TableRow key={row.id}><TableCell>{dateTime(row.created_at)}</TableCell><TableCell>{row.status}</TableCell><TableCell>{row.turn_id ? <a className="ns-link" href={`#${automation.session_id}`}>{row.turn_id}</a> : "No turn admitted"}</TableCell></TableRow>)}</RecordTable> : <p className="ns-muted">No recorded runs yet.</p>}</LoadState>
    <Button variant="outline" disabled={resource.loading} onClick={resource.refresh}><RefreshCw />Refresh history</Button>
  </DialogContent></Dialog>
}

function AutomationsPage({ user }: { user: SettingsUser }) {
  const resource = useResource<Collection & { automations: Automation[] }>("/settings/automations")
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [create, setCreate] = useState(false), [remove, setRemove] = useState<Automation | null>(null), [logs, setLogs] = useState<Automation | null>(null)
  const mutation = useMutation()
  return <LoadState resource={resource}>{data => {
    const rows = data.automations.filter(row => contains(search, row.name, row.prompt) && (filter === "all" || (filter === "paused" ? row.paused : !row.paused)))
    return <><CapabilityNote {...data} /><Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
      filter={<Choice label="Automation state" value={filter} onChange={setFilter} options={[{ value: "all", label: "All automations" }, { value: "active", label: "Not paused" }, { value: "paused", label: "Paused" }]} />}
      action={data.capabilities?.create && <Button onClick={() => setCreate(true)}><Plus />New automation</Button>} />
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      {rows.length ? <div className="ns-records">{rows.map(row => <article key={row.id} className="ns-record"><CalendarClock className="ns-record-icon" aria-hidden="true" /><div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.paused ? "Paused" : row.state}</span></div>
        <p>Every {count(row.interval_minutes)} minutes · {row.model}</p><details><summary>View instructions</summary><pre>{row.prompt}</pre></details><a className="ns-link" href={`#${row.session_id}`}>Open session</a></div>
        <div className="ns-row-actions"><Button variant="outline" size="sm" onClick={() => setLogs(row)}>Run history<span className="visually-hidden"> for {row.name}</span></Button>
          {data.capabilities?.update && <Button variant="outline" size="sm" disabled={mutation.busy} onClick={() => void mutation.run(() => api(`/settings/automations/${encodeURIComponent(row.id)}`, "PATCH", { paused: !row.paused }), resource.refresh)}>{row.paused ? "Resume" : "Pause"}<span className="visually-hidden"> {row.name}</span></Button>}
          {data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} disabled={mutation.busy} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
      </article>)}</div> : <Empty filtered={Boolean(search) || filter !== "all"} noun="automations" onClear={() => { setSearch(""); setFilter("all") }} />}
      {create && <AutomationEditor user={user} refresh={resource.refresh} onClose={() => setCreate(false)} />}
      {logs && <AutomationLogs automation={logs} onClose={() => setLogs(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="Future scheduled admissions stop. Work already admitted to a session and its history are kept." label="Delete automation" onConfirm={async () => { await api(`/settings/automations/${encodeURIComponent(remove.id)}`, "DELETE"); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

type AnalyticsSummary = {
  turns: number; sessions?: number; completed: number; failed: number; input_tokens: number; output_tokens: number; total_tokens: number; requests: number
  cost_usd: number | null; priced_turns: number; unpriced_turns: number; cost_coverage: number | null
  latency_samples: number; mean_latency_ms: number | null; p50_latency_ms: number | null; p95_latency_ms: number | null
}
type AnalyticsRow = {
  id: string; user_id: string; model: string; status: string; created_at: string; accepted_at?: string; finished_at?: string
  input_tokens: number; output_tokens: number; total_tokens: number; requests: number; tool_calls: number; cache_read_tokens: number; cache_write_tokens: number
  queue_ms: number | null; latency_ms: number | null; total_ms: number | null; cost_usd: number | null; pricing_status: string
}
type Analytics = {
  rows: AnalyticsRow[]; summary: AnalyticsSummary; series: (AnalyticsSummary & { date: string })[]
  by_user?: (AnalyticsSummary & { user_id: string })[]
  models: string[]; users: Account[]; total: number; limit: number; offset: number; start: string; end: string; cost_note: string; latency_note: string
}
type ReportFilters = { start: string; end: string; model: string; status: string; user_id: string }
const duration = (value: number | null | undefined) => typeof value !== "number" || !Number.isFinite(value) ? "Unavailable" : value < 1000 ? `${Math.round(value)} ms` : value < 60000 ? `${(value / 1000).toFixed(1)} s` : `${(value / 60000).toFixed(1)} min`
const money = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) ? new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 }).format(value) : "Unavailable"
function reportDefaults(): ReportFilters {
  const now = new Date(), start = new Date(now)
  start.setUTCDate(start.getUTCDate() - 6)
  return { start: start.toISOString().slice(0, 10), end: now.toISOString().slice(0, 10), model: "all", status: "all", user_id: "all" }
}
function reportParams(filters: ReportFilters) {
  const end = new Date(`${filters.end}T00:00:00Z`)
  end.setUTCDate(end.getUTCDate() + 1)
  return new URLSearchParams({
    start: `${filters.start}T00:00:00Z`, end: end.toISOString(),
    ...(filters.model !== "all" ? { model: filters.model } : {}),
    ...(filters.status !== "all" ? { status: filters.status } : {}),
    ...(filters.user_id !== "all" ? { user_id: filters.user_id } : {}),
  })
}

function Metrics({ items }: { items: { label: string; value: string; note?: string }[] }) {
  return <dl className="ns-metrics">{items.map(item => <div key={item.label}><dt>{item.label}</dt><dd>{item.value}</dd>{item.note && <p>{item.note}</p>}</div>)}</dl>
}

function Trend({ title, points, format }: { title: string; points: { date: string; value: number | null }[]; format: (value: number | null) => string }) {
  const values = points.flatMap(point => point.value !== null && Number.isFinite(point.value) ? [point.value] : [])
  const highest = Math.max(1, ...values), width = 720, height = 176, step = (width - 32) / Math.max(points.length, 1)
  return <section className="ns-chart-section"><div className="ns-section-heading"><h2>{title}</h2><span className="ns-muted">Daily · UTC</span></div>
    {values.length ? <><svg className="ns-chart" viewBox={`0 0 ${width} ${height}`} role="img" aria-label={`${title}. Exact values are available in the table below.`}>
      <line className="ns-chart-axis" x1={16} x2={width - 16} y1={height - 20} y2={height - 20} />
      {points.map((point, index) => {
        if (point.value === null || !Number.isFinite(point.value)) return null
        const barHeight = point.value / highest * (height - 40), barWidth = Math.min(48, Math.max(1, step * .7))
        return <rect key={point.date} x={16 + index * step + (step - barWidth) / 2} y={height - 20 - barHeight} width={barWidth} height={barHeight} rx={Math.min(3, step / 6)}><title>{point.date}: {format(point.value)}</title></rect>
      })}
    </svg><div className="ns-chart-range"><span>{points[0]?.date}</span><span>Scale: 0–{format(highest)}</span><span>{points.at(-1)?.date}</span></div></>
      : <p className="ns-chart-empty">No recorded values for this chart in the selected range. Missing measurements are not treated as zero.</p>}
    <details className="ns-chart-values"><summary>View daily values</summary>
      {points.length ? <RecordTable label={`${title}: daily values`} headings={["Date (UTC)", title]}>{points.map(point => <TableRow key={point.date}><TableCell>{point.date}</TableCell><TableCell>{format(point.value)}</TableCell></TableRow>)}</RecordTable> : <p className="ns-muted">No daily records in this range.</p>}
    </details>
  </section>
}

function AnalyticsPage({ kind }: { kind: "performance" | "usage" }) {
  const [filters, setFilters] = useState(reportDefaults), [draft, setDraft] = useState(reportDefaults)
  const [offset, setOffset] = useState(0), [filterError, setFilterError] = useState("")
  const [models, setModels] = useState<string[]>([]), [users, setUsers] = useState<Account[]>([])
  const [reportView, setReportView] = useState<"overall" | "users" | "history">("overall")
  const [metric, setMetric] = useState<"requests" | "total_tokens" | "cost_usd" | "mean_latency_ms" | "p95_latency_ms">(kind === "performance" ? "mean_latency_ms" : "requests")
  const params = reportParams(filters)
  const resource = useResource<Analytics>(`/settings/${kind}?${params}&limit=50&offset=${offset}`)
  useEffect(() => {
    if (!resource.data) return
    setModels(previous => [...new Set([...previous, ...resource.data!.models])].sort())
    setUsers(resource.data.users)
  }, [resource.data])
  const updateDraft = (key: keyof ReportFilters, value: string) => setDraft(previous => ({ ...previous, [key]: value }))
  const format = metric === "cost_usd" ? money : metric.endsWith("_ms") ? duration : count
  const metricOptions = kind === "performance"
    ? [{ value: "mean_latency_ms", label: "Mean turn latency" }, { value: "p95_latency_ms", label: "p95 turn latency" }] as const
    : [{ value: "requests", label: "Model requests" }, { value: "total_tokens", label: "Tokens" }, { value: "cost_usd", label: "Recorded cost" }] as const
  const reset = () => { const initial = reportDefaults(); setDraft(initial); setFilters(initial); setOffset(0); setFilterError("") }
  const reportViews = [{ id: "overall", label: "Overall" }, { id: "users", label: "Users" }, { id: "history", label: "Usage history" }] as const
  const metricControls = <div className="ns-chart-tabs" role="group" aria-label="Chart metric">{metricOptions.map(option => <Button key={option.value} type="button" variant={metric === option.value ? "secondary" : "ghost"} aria-pressed={metric === option.value} onClick={() => setMetric(option.value)}>{option.label}</Button>)}</div>
  const csvLabel = kind === "usage" && reportView === "users" ? "Export turns CSV" : "Export CSV"
  return <>
    <form className="ns-report-filters" onSubmit={event => {
      event.preventDefault()
      const start = Date.parse(`${draft.start}T00:00:00Z`), end = Date.parse(`${draft.end}T00:00:00Z`)
      if (!Number.isFinite(start) || !Number.isFinite(end) || end < start || end - start >= 366 * 86400000) { setFilterError("Choose an ordered date range of at most 366 days."); return }
      setFilterError(""); setFilters({ ...draft }); setOffset(0)
    }}>
      <label>Start date (UTC)<Input type="date" required value={draft.start} onChange={event => updateDraft("start", event.target.value)} /></label>
      <label>End date (UTC)<Input type="date" required value={draft.end} min={draft.start} onChange={event => updateDraft("end", event.target.value)} /></label>
      <label>Model<Choice label="Report model" value={draft.model} onChange={value => updateDraft("model", value)} options={[{ value: "all", label: "All models" }, ...models.map(value => ({ value, label: value }))]} /></label>
      <label>User<Choice label="Report user" value={draft.user_id} onChange={value => updateDraft("user_id", value)} options={[{ value: "all", label: "All users" }, ...users.map(row => ({ value: row.id, label: row.display_name || row.username }))]} /></label>
      <label>Status<Choice label="Report status" value={draft.status} onChange={value => updateDraft("status", value)} options={["all", "queued", "starting", "running", "waiting", "saving", "steering", "signal_sent", "accepted", "included", "unconfirmed", "resumed", "completed", "failed", "stopped"].map(value => ({ value, label: value === "all" ? "All statuses" : (value[0].toUpperCase() + value.slice(1)).replaceAll("_", " ") }))} /></label>
      <div className="ns-filter-actions"><Button type="submit">Apply filters</Button><Button type="button" variant="ghost" onClick={reset}>Reset</Button></div>
      <p className="ns-field-hint">Both dates are included. Reports use UTC days; row timestamps use your local time.</p>
      {filterError && <p className="ns-error" role="alert">{filterError}</p>}
    </form>
    <div className="ns-report-actions">{kind === "usage" ? <div className="ns-report-tabs" role="tablist" aria-label="Spend and usage view">{reportViews.map((view, index) => <Button key={view.id} id={`ns-report-tab-${view.id}`} type="button" role="tab" aria-selected={reportView === view.id} aria-controls="ns-report-panel" tabIndex={reportView === view.id ? 0 : -1} variant={reportView === view.id ? "secondary" : "ghost"} onClick={() => setReportView(view.id)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
      event.preventDefault()
      const next = event.key === "Home" ? 0 : event.key === "End" ? reportViews.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + reportViews.length) % reportViews.length
      setReportView(reportViews[next].id)
      event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
    }}>{view.label}</Button>)}</div> : metricControls}
      <div><Button variant="outline" disabled={resource.loading} onClick={resource.refresh}><RefreshCw />Refresh</Button>
        {resource.data && !resource.loading && !resource.error ? <Button variant="outline" asChild><a href={`/api/settings/${kind}?${params}&format=csv`} download><Download />{csvLabel}</a></Button> : <Button variant="outline" disabled><Download />{csvLabel}</Button>}</div>
    </div>
    <div id="ns-report-panel" role={kind === "usage" ? "tabpanel" : undefined} aria-labelledby={kind === "usage" ? `ns-report-tab-${reportView}` : undefined}>
    <LoadState resource={resource}>{data => <>
      <p className="ns-notice">{kind === "performance" ? data.latency_note : data.cost_note}</p>
      {kind === "usage" && reportView === "overall" && <Metrics items={[
        { label: "Recorded cost", value: money(data.summary.cost_usd), note: data.summary.cost_usd === null ? "No authoritative cost recorded" : data.summary.unpriced_turns > 0 ? "Partial coverage" : "Recorded USD" },
        { label: "Cost coverage", value: `${count(data.summary.priced_turns)} / ${count(data.summary.turns)} turns`, note: `${count(data.summary.unpriced_turns)} unpriced` },
        { label: "Sessions", value: count(data.summary.sessions), note: "Distinct sessions in this range" },
        { label: "Model requests", value: count(data.summary.requests) }, { label: "Tokens", value: count(data.summary.total_tokens), note: `${count(data.summary.input_tokens)} input · ${count(data.summary.output_tokens)} output` },
      ]} />}
      {kind === "performance" && <Metrics items={[
        { label: "Mean turn latency", value: duration(data.summary.mean_latency_ms) }, { label: "Median (p50)", value: duration(data.summary.p50_latency_ms) },
        { label: "p95", value: duration(data.summary.p95_latency_ms) }, { label: "Measured turns", value: `${count(data.summary.latency_samples)} / ${count(data.summary.turns)}`, note: "Missing timings are excluded" },
      ]} />}
      {(kind === "performance" || reportView === "overall") && <>{kind === "usage" && metricControls}<Trend title={metricOptions.find(option => option.value === metric)?.label ?? "Recorded values"} points={data.series.map(day => ({ date: day.date, value: day[metric] }))} format={format} /></>}
      {kind === "usage" && reportView === "users" && <section><div className="ns-section-heading"><h2>Usage by user</h2><span className="ns-muted">Entire filtered range</span></div><p className="ns-muted ns-section-description">Totals include all matching turns, regardless of table pagination. Costs without an authoritative value remain unavailable.</p>
        {!data.by_user ? <p className="ns-notice">User aggregates are not available from this server.</p> : data.by_user.length ? <RecordTable label="Usage by user" headings={["User", "Sessions", "Turns", "Requests", "Tokens", "Recorded cost"]}>
          {data.by_user.map(row => { const user = data.users.find(user => user.id === row.user_id); return <TableRow key={row.user_id}>
            <TableCell><strong>{user?.display_name || user?.username || row.user_id}</strong>{user?.username && <small>{user.username}</small>}</TableCell><TableCell>{count(row.sessions)}</TableCell><TableCell>{count(row.turns)}</TableCell><TableCell>{count(row.requests)}</TableCell><TableCell>{count(row.total_tokens)}<small>{count(row.input_tokens)} in · {count(row.output_tokens)} out</small></TableCell>
            <TableCell>{money(row.cost_usd)}<small>{count(row.priced_turns)} priced · {count(row.unpriced_turns)} unpriced turns</small></TableCell>
          </TableRow> })}
        </RecordTable> : <Empty filtered noun="user totals" onClear={reset} />}
      </section>}
      {(kind === "performance" || reportView === "history") && <><div className="ns-section-heading"><h2>Recorded turns</h2><span className="ns-muted">{count(data.summary.completed)} completed · {count(data.summary.failed)} failed</span></div>
      {data.rows.length ? <RecordTable label={`${kind === "performance" ? "Performance" : "Usage"} by turn`} headings={["Turn / created", "User", "Model / status", ...(kind === "performance" ? ["Queue wait", "Turn latency", "Total time"] : ["Requests", "Tokens", "Recorded cost"])]}>
        {data.rows.map(row => <TableRow key={row.id}>
          <TableCell><details><summary>{row.id.slice(0, 8)}</summary><code>{row.id}</code><small>Tool calls: {count(row.tool_calls)}</small><small>Cache read: {count(row.cache_read_tokens)} · write: {count(row.cache_write_tokens)}</small></details><small>{dateTime(row.created_at)}</small></TableCell>
          <TableCell>{data.users.find(user => user.id === row.user_id)?.display_name || data.users.find(user => user.id === row.user_id)?.username || row.user_id}</TableCell>
          <TableCell>{row.model}<small>{row.status}</small></TableCell>
          {kind === "performance" ? <><TableCell>{duration(row.queue_ms)}</TableCell><TableCell>{duration(row.latency_ms)}</TableCell><TableCell>{duration(row.total_ms)}</TableCell></>
            : <><TableCell>{count(row.requests)}</TableCell><TableCell>{count(row.total_tokens)}<small>{count(row.input_tokens)} in · {count(row.output_tokens)} out</small></TableCell><TableCell>{money(row.cost_usd)}<small>{row.pricing_status}</small></TableCell></>}
        </TableRow>)}
      </RecordTable> : <Empty filtered noun="turns" onClear={reset} />}
      <div className="ns-pagination"><p className="ns-muted" role="status">{data.total ? `${count(data.offset + 1)}–${count(Math.min(data.offset + data.rows.length, data.total))} of ${count(data.total)} turns` : "No turns in this range"}</p>
        <div><Button variant="outline" disabled={resource.loading || data.offset === 0} onClick={() => setOffset(Math.max(0, data.offset - data.limit))}>Previous</Button><Button variant="outline" disabled={resource.loading || data.offset + data.limit >= data.total} onClick={() => setOffset(data.offset + data.limit)}>Next</Button></div></div></>}
      <details className="ns-method"><summary>About this data</summary><p>{data.cost_note}</p><p>{data.latency_note}</p><p>Charts, session counts, and user totals cover the entire filtered range. Usage history is paginated; the CSV includes all matching turns. Narrow the range if the server’s reporting limit is reached.</p><p>PR analytics, provider invoices, and leaderboard rankings are not instrumented in this report.</p></details>
    </>}</LoadState>
    </div>
  </>
}

function OverviewPage({ user, navigate }: { user: SettingsUser; navigate: (page: string) => void }) {
  const resource = useResource<{ counts: Record<string, number>; capabilities: Record<string, Capabilities | boolean> }>("/settings/overview")
  return <>
    {resource.error && <p className="ns-notice" role="alert">Overview counts could not be loaded. {resource.error} <Button variant="ghost" onClick={resource.refresh}>Try again</Button></p>}
    {resource.loading && <p className="ns-muted" role="status">Loading overview counts…</p>}
    <div className="ns-overview-grid">{groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section key={group} className="ns-overview-group"><div className="ns-section-heading"><h2>{group}</h2>{group === "Administration" && <span className="ns-badge">Admin</span>}</div>
      <div>{destinations.filter(item => item.group === group).map(item => <SettingsLink key={item.id} page={item.id} navigate={navigate} className="ns-overview-link"><item.Icon size={20} aria-hidden="true" /><span><strong>{item.title}</strong><span>{item.description}</span></span>
        {resource.data?.counts[item.id] !== undefined && <small>{count(resource.data.counts[item.id])}</small>}<ChevronRight size={18} aria-hidden="true" /></SettingsLink>)}</div>
    </section>)}</div>
  </>
}

function SettingsLink({ page, navigate, children, ...props }: { page: string; navigate: (page: string) => void; children: React.ReactNode } & Omit<React.ComponentProps<"a">, "href" | "onClick">) {
  return <a {...props} href={`#settings/${page}`} onClick={event => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.altKey || event.shiftKey) return
    event.preventDefault(); navigate(page)
  }}>{children}</a>
}

export function SettingsPage({ user, page, onNavigate, onBack }: SettingsPageProps) {
  const current = !page || page === "settings" ? "overview" : page === "spend" ? "usage" : page
  const destination = destinations.find(item => item.id === current)
  const [navOpen, setNavOpen] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const title = current === "overview" ? "Settings" : destination?.title ?? "Page not found"
  useEffect(() => {
    setNavOpen(false); heading.current?.focus(); content.current?.scrollTo({ top: 0 })
    const previous = document.title
    document.title = `${title} · Moyai`
    return () => { document.title = previous }
  }, [current, title])
  const navigate = (value: string) => { setNavOpen(false); onNavigate(value) }
  const navLink = (value: string, label: string, icon: React.ReactNode) => <SettingsLink page={value} navigate={navigate} aria-current={current === value ? "page" : undefined}>{icon}<span>{label}</span></SettingsLink>
  return <div className="native-settings">
    <a className="ns-skip" href="#native-settings-content" onClick={event => { event.preventDefault(); heading.current?.focus() }}>Skip to settings content</a>
    <aside className="ns-rail">
      <div className="ns-brand-row"><a href="#" className="ns-brand" onClick={event => { if (!event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onBack() } }}><img src={logo} width={32} height={32} alt="" /><span>Moyai<small>LiteLLM</small></span></a><Button className="ns-nav-toggle" size="icon" variant="ghost" aria-label="Toggle settings navigation" aria-expanded={navOpen} aria-controls="native-settings-navigation" onClick={() => setNavOpen(value => !value)}><Menu /></Button></div>
      <Button className="ns-back" variant="ghost" onClick={onBack}><ArrowLeft size={16} />Back to workspace</Button>
      <nav id="native-settings-navigation" className={navOpen ? "ns-navigation open" : "ns-navigation"} aria-label="Settings">
        <p className="ns-nav-title">Settings</p>{navLink("overview", "Overview", <LayoutGrid size={17} aria-hidden="true" />)}
        {groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section className="ns-nav-group" key={group}><h2>{group}</h2>{destinations.filter(item => item.group === group).map(item => <React.Fragment key={item.id}>{navLink(item.id, item.title, <item.Icon size={17} aria-hidden="true" />)}</React.Fragment>)}</section>)}
      </nav><div className="ns-account"><span>{user.name}</span><small>{user.role === "admin" ? "Administrator" : "Workspace member"}</small></div>
    </aside>
    <div className="ns-main"><div className="ns-breadcrumb" aria-label="Breadcrumb"><SettingsLink page="overview" navigate={navigate}>Settings</SettingsLink>{current !== "overview" && <><ChevronRight size={14} aria-hidden="true" /><span>{title}</span></>}</div>
      <div className="ns-content" id="native-settings-content" ref={content}><header className="ns-page-heading"><h1 ref={heading} tabIndex={-1}>{title}</h1><p>{current === "overview" ? "Make Moyai work the way your team does." : destination?.description}</p></header>
        <div key={`${user.id}:${current}`}>
          {destination?.admin && user.role !== "admin" ? <div className="ns-empty" role="alert"><Shield size={24} /><h2>Administrator access required</h2><p>This page is available to workspace administrators.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview</Button></div>
            : current === "overview" ? <OverviewPage user={user} navigate={navigate} />
              : current === "skills" ? <SkillsPage user={user} />
                : current === "automations" ? <AutomationsPage user={user} />
                  : current === "memory" ? <MemoryPage />
                    : current === "connections" ? <ConnectionsPage user={user} />
                      : current === "secrets" ? <SecretsPage user={user} />
                        : current === "users" ? <UsersPage user={user} />
                          : current === "performance" || current === "usage" ? <AnalyticsPage kind={current} />
                            : <div className="ns-empty"><p>This settings page does not exist.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview<ArrowRight /></Button></div>}
        </div>
      </div>
    </div>
  </div>
}
