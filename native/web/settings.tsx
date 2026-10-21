import React, { useCallback, useEffect, useId, useRef, useState } from "react"
import { ArrowLeft, ArrowRight, BookOpen, CalendarClock, ChevronRight, Gauge, KeyRound, LayoutGrid, Link2, LockKeyhole, Menu, Pencil, Plus, RefreshCw, Search, Shield, Slack, SlidersHorizontal, FileText, Trash2, Users, Wallet, X } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Checkbox } from "@/components/ui/checkbox"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { ApiError, csrfToken } from "./api"
import { createConnection, listConnections, listSessions, updateConnection, type ConnectionInputWritable, type ConnectionPolicy, type ConnectionView } from "./generated/api"
import { AnalyticsPage } from "./analytics"
import { ProductAutomationsPage } from "./product-automations"
import { onceUtcInput, onceUtcInstant } from "./product-automation-data"
import { listSettingsUsers, updateProductUserRole, type ProductDirectoryUser, type SettingsAccount } from "./generated/api"
import {
  checkPersonalSlack, connectExistingGitHubApp, disconnectPersonalSlack, getMe, getPersonalSlack, listGitHubRepositories,
  selectGitHubRepositories, startGitHubConnection, startPersonalSlackOAuth,
  getPersonalGranola, startPersonalGranolaOAuth, disconnectPersonalGranola,
} from "./generated/api"
import { MoyaiBrand, SidebarToggle } from "./brand"
import { AppearanceToggle } from "./appearance-toggle"
import type { GeneralPreferences } from "./general-preferences"
import {
  checkOrganizationConnection, connectOrganizationConnection, createCredential, createProductMemory, deleteProductMemory,
  disconnectOrganizationConnection, getProductSkill, listCredentials, listOrganizationConnections, listProductMemories,
  listProductSkills, revokeCredential, startOrganizationConnectionOAuth, updateCredential, updateOrganizationConnectionPolicy,
  updateProductMemory, updateProductMemoryPreferences,
  type CredentialInventory, type CredentialProvider, type CredentialSecretMetadata, type Note as ProductMemoryWriteBody,
  type OrganizationConnection, type ProductMemoryNote as ProductMemoryNoteData, type ProductSaveSecretWritable,
  type ProductSkillDetail, type ProductSkillLibrary, type ProductSkillSummary, type ProductUpdateSecretWritable,
  createSettingsAutomation, createSettingsSecret, createSettingsSkill, createSettingsUser, deleteSettingsAutomation, deleteSettingsMemory,
  deleteSettingsSecret, deleteSettingsSkill, deleteSettingsUser, getSettingsGeneral, getSettingsOverview, listSettingsAutomationLogs,
  listSettingsAutomations, listSettingsMemory, listSettingsSecrets, listSettingsSkills, updateSettingsAutomation, updateSettingsGeneral,
  updateSettingsSecret, updateSettingsSkill, updateSettingsUser, writeSettingsMemory,
  type SecretEditWritable, type SettingsAutomation, type SettingsMemoryFile, type SettingsSecret, type SettingsSkill, type UserEditWritable,
} from "./generated/api"
import { listHistorySessions, type HistorySessionList, type SourcedHistorySession } from "./generated/api"
import "./settings.css"

type SettingsUser = { id: string; name: string; role: string; individual?: boolean }
type SettingsPageProps = { user: SettingsUser; page: string; productMode?: "standalone" | "existing"; onNavigate: (page: string) => void; onBack: () => void; onStartPrivateChat: () => void; sidebarCollapsed: boolean; onToggleSidebar: () => void; onPreferences: (preferences: GeneralPreferences) => void }
type Capabilities = { read?: boolean; create?: boolean; update?: boolean; delete?: boolean; roles?: boolean; preferences?: boolean; review?: boolean; reason?: string; runtime_available?: boolean; shared?: boolean; run?: boolean; agent_access?: boolean }
type Collection = { capabilities?: Capabilities; reason?: string }
type CredentialDraft = {
  provider: CredentialProvider["id"]; label: string; name: string; format: "env" | "file"; env_var: string
  scope: string; lifetime: string; root_id: string; expires_at: string; value: string
}
/** UI view of a saved note: editing stays disabled for a truncated preview, and a confirmed repository ID is kept on resave. */
type ProductMemoryNote = ProductMemoryNoteData & { repository_id?: number | null; truncated?: boolean }
/** A row of either directory: native accounts, or product members (who also carry email and role revision). */
type Account = Pick<SettingsAccount, "id" | "username" | "display_name" | "role" | "enabled"> & Partial<Pick<ProductDirectoryUser, "email" | "revision">>
type UserDirectory = { users: Account[]; product: boolean; note?: string } & Collection
async function loadUsers(signal: AbortSignal): Promise<UserDirectory> {
  const { data } = await listSettingsUsers({ signal })
  return "source" in data
    ? { users: data.users, product: true, note: data.note, capabilities: data.capabilities }
    : { users: data.users, product: false, capabilities: data.capabilities }
}
type Connection = ConnectionView
const loadConnections = async (signal: AbortSignal) => (await listConnections({ signal })).data
const loadSessions = async (signal: AbortSignal) => (await listSessions({ signal })).data
const connectionKinds = ["github", "linear", "notion", "slack"] as const
function connectionInput(values: FormValues, policy: ConnectionPolicy): ConnectionInputWritable {
  const kind = connectionKinds.find(item => item === values.kind)
  if (!kind) throw new Error("Choose a supported service.")
  return {
    name: String(values.name ?? ""), kind, credential: String(values.credential ?? ""),
    shared: values.shared === true, read_only: values.read_only === true, enabled: values.enabled === true, policy,
  }
}
type Option = { value: string; label: string }
type Resource<T> = { data: T | undefined; loading: boolean; error: string; refresh: () => void }
type FormValues = Record<string, string | number | boolean>
type Field = { name: string; label: string; type?: "text" | "password" | "textarea" | "select" | "checkbox" | "number" | "url"; required?: boolean; hint?: string; options?: Option[]; disabled?: boolean; min?: number; max?: number; minLength?: number; maxLength?: number; pattern?: string }

const destinations = [
  { id: "general", title: "General", description: "Set your chat preferences.", group: "Preferences", Icon: SlidersHorizontal },
  { id: "skills", title: "Skills", description: "Choose reusable instructions for your chats.", group: "Workflows", Icon: FileText },
  { id: "automations", title: "Automations", description: "Schedule recurring work and review past runs.", group: "Workflows", Icon: CalendarClock },
  { id: "memory", title: "Memory", description: "Manage what Moyai remembers about your work.", group: "Workflows", Icon: BookOpen },
  { id: "connections", title: "Connections", description: "Choose the accounts Moyai can use in your sessions.", group: "Integrations", Icon: Link2 },
  { id: "granola", title: "Granola", description: "Read your Granola meeting notes in private chats.", group: "Integrations", Icon: FileText },
  { id: "personal-slack", title: "Personal Slack", description: "Use your Slack account in private chats with Moyai.", group: "Integrations", Icon: LockKeyhole, productOnly: true },
  { id: "secrets", title: "Secrets", description: "Manage personal and shared service credentials.", group: "Integrations", Icon: KeyRound },
  { id: "users", title: "Users", description: "Manage workspace members, roles, and access.", group: "Administration", Icon: Users, admin: true },
  { id: "performance", title: "Performance", description: "Review recorded turn timings across the workspace.", group: "Administration", Icon: Gauge, admin: true },
  { id: "usage", title: "Spend & usage", description: "Review recorded model usage and cost coverage.", group: "Administration", Icon: Wallet, admin: true },
]
const groups = ["Preferences", "Workflows", "Integrations", "Administration"]
const scopeOptions = [{ value: "all", label: "All scopes" }, { value: "personal", label: "Personal" }, { value: "shared", label: "Shared" }, { value: "bundled", label: "Bundled" }]
const scopes = (user: SettingsUser, capabilities?: Capabilities) => scopeOptions.filter(option => option.value === "personal" || (user.role === "admin" && capabilities?.shared === true && option.value === "shared"))
const contains = (query: string, ...values: (string | undefined)[]) => values.join(" ").toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
const count = (value: number | null | undefined) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString() : "Unavailable"
const dateTime = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not recorded"
const errorText = (error: unknown) => error instanceof ApiError && error.status === 404
  ? "This settings endpoint is not available on this server."
  : error instanceof Error ? error.message : "The request could not be completed."

const scopeInput = (value: FormValues[string]) => value === "shared" ? "shared" as const : "personal" as const

/** `path` keys the read (and its cache); `load` performs it with a generated SDK call. */
function useResource<T>(path: string | null, load: (signal: AbortSignal) => Promise<T>): Resource<T> {
  const loader = useRef(load)
  loader.current = load
  const [revision, setRevision] = useState(0)
  const [state, setState] = useState<{ path: string | null; data?: T; loading: boolean; error: string }>({ path, loading: Boolean(path), error: "" })
  useEffect(() => {
    if (!path) { setState({ path, loading: false, error: "" }); return }
    const controller = new AbortController()
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true, error: "" }))
    void loader.current(controller.signal).then(
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
    const requestAuth = csrfToken()
    pending.current = true; setBusy(true); setError("")
    try { await action(); if (alive.current && csrfToken() === requestAuth) done() }
    catch (error) {
      if (alive.current && csrfToken() === requestAuth) {
        setError(errorText(error))
        if (error instanceof ApiError && error.status === 403) window.dispatchEvent(new Event("moyai:access-changed"))
      }
    }
    finally { pending.current = false; if (alive.current && csrfToken() === requestAuth) setBusy(false) }
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
      {search && <Button type="button" variant="ghost" size="icon-sm" aria-label="Clear search" onClick={() => setSearch("")}><X /></Button>}</div>
    {filter}{total !== undefined && <span className="ns-count">{count(total)}</span>}
    <Button type="button" variant="outline" disabled={resource.loading} onClick={resource.refresh} aria-label="Refresh settings"><RefreshCw size={16} /><span>Refresh</span></Button>
    {action}
  </div>
}

function Empty({ filtered, noun, onClear }: { filtered: boolean; noun: string; onClear: () => void }) {
  return <div className="ns-empty"><h2>{filtered ? `No matching ${noun}` : `No ${noun} yet`}</h2>
    {filtered && <p>Try another search or clear the filters.</p>}
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
  const resource = useResource("/settings/skills", signal => listSettingsSkills({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<SettingsSkill | "new" | null>(null), [remove, setRemove] = useState<SettingsSkill | null>(null)
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
        onSave={async values => {
          const body = { name: String(values.name), description: String(values.description), content: String(values.content), scope: scopeInput(values.scope), enabled: values.enabled === true }
          if (editor === "new") await createSettingsSkill({ body })
          else await updateSettingsSkill({ path: { identity: editor.id }, body })
          resource.refresh()
        }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="This removes the saved skill. Existing conversations are kept." label="Delete skill" onConfirm={async () => { await deleteSettingsSkill({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function SecretsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/secrets", async signal => {
    const { data } = await listSettingsSecrets({ signal })
    // Product mode answers with the credential inventory; that mode renders ProductSecretsPage.
    if ("providers" in data) throw new Error("Saved secrets are managed under Credentials.")
    return data
  })
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<SettingsSecret | "new" | null>(null), [remove, setRemove] = useState<SettingsSecret | null>(null)
  return <LoadState resource={resource}>{data => {
    const rows = data.secrets.filter(row => contains(search, row.name, row.description) && (scope === "all" || row.scope === scope))
    const editable = (row: SettingsSecret) => row.editable !== false && (row.scope === "personal" || user.role === "admin")
    return <><p className="ns-help">For GitHub, Slack, and other supported services, use <a className="ns-link" href="#settings/connections">Connections</a>.</p>
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
      ]} onSave={async values => {
        const fields = { name: String(values.name), description: String(values.description), scope: scopeInput(values.scope) }
        if (editor === "new") await createSettingsSecret({ body: { ...fields, value: String(values.value) } })
        else {
          const body: SecretEditWritable = values.value ? { ...fields, value: String(values.value) } : fields
          await updateSettingsSecret({ path: { identity: editor.id }, body })
        }
        resource.refresh()
      }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="Future work that needs this credential may fail until a replacement is saved." label="Delete secret" onConfirm={async () => { await deleteSettingsSecret({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

export function ProductSecretRows({ secrets, providers, capabilities, onEdit, onRevoke }: {
  secrets: CredentialSecretMetadata[]; providers: { id: string; name: string }[]; capabilities?: CredentialInventory["capabilities"]
  onEdit?: (secret: CredentialSecretMetadata) => void; onRevoke?: (secret: CredentialSecretMetadata) => void
}) {
  return <RecordTable label="Saved credentials" headings={["Credential", "Scope", "Lifetime", "Status", "Value", "Actions"]}>{secrets.map(secret => <TableRow key={secret.id}>
    <TableCell><strong>{secret.label}</strong><small>{secret.provider === "generic" ? secret.name : providers.find(provider => provider.id === secret.provider)?.name || secret.provider}</small>
      <small>{secret.format === "aws-sso" ? "AWS SSO login" : secret.format === "file" ? "Credential file" : secret.provider === "generic" ? "Environment variables" : "Provider access"} · Added {dateTime(secret.created_at)}</small>
    </TableCell><TableCell>{secret.scope === "organization" ? "Organization" : "Personal"}</TableCell>
    <TableCell>{secret.lifetime === "session" || secret.scope === "session" ? "This session only" : "Future sessions"}{secret.expires_at && <small>Expires {dateTime(secret.expires_at)}</small>}</TableCell>
    <TableCell>{({ active: "Active", expired: "Expired", invalid: "Needs updating", revoked: "Revoked" } as Record<string, string>)[secret.status] || secret.status}</TableCell>
    <TableCell><span aria-label="Value hidden">••••••••</span></TableCell>
    <TableCell><div className="ns-row-actions">
      {capabilities?.update && secret.can_manage && secret.format !== "aws-sso" && onEdit && <Button variant="outline" size="sm" onClick={() => onEdit(secret)}>Edit<span className="visually-hidden"> {secret.label}</span></Button>}
      {capabilities?.delete && secret.can_manage && onRevoke && <Button variant="ghost" size="sm" onClick={() => onRevoke(secret)}>Revoke access<span className="visually-hidden"> for {secret.label}</span></Button>}
    </div>{secret.format === "aws-sso" && <small>AWS SSO access is renewed through its sign-in flow.</small>}</TableCell>
  </TableRow>)}</RecordTable>
}
type CredentialRootSession = Pick<SourcedHistorySession, "id" | "source" | "title" | "parent_session_id" | "deleted" | "deleting">
/** One page of session-lifetime credential roots from the history listing. */
function useHistoryRoots(enabled: boolean, cursor: string | null) {
  const [revision, setRevision] = useState(0)
  const key = enabled ? `${revision}:${cursor ?? ""}` : null
  const [state, setState] = useState<{ key: string | null; data?: HistorySessionList; error: string }>({ key: null, error: "" })
  useEffect(() => {
    if (key === null) return
    const controller = new AbortController()
    void listHistorySessions({ query: { source: "all", limit: 100, cursor: cursor ?? undefined }, signal: controller.signal }).then(
      ({ data }) => { if (!controller.signal.aborted) setState({ key, data, error: "" }) },
      error => { if (!controller.signal.aborted) setState({ key, error: errorText(error) }) },
    )
    return () => controller.abort()
  }, [key])
  const refresh = useCallback(() => setRevision(value => value + 1), [])
  const current = state.key === key
  return { data: current ? state.data : undefined, loading: key !== null && !current, error: current ? state.error : "", refresh }
}

export function productCredentialRoot(session: { id: string; source: string }): string {
  if (session.source === "legacy" && /^[0-9a-f]{32}$/.test(session.id)) return session.id
  if (session.source === "native" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(session.id)) return `native:${session.id}`
  throw new Error("Choose an available session from the list.")
}
const credentialScope = (value: string): value is "personal" | "organization" => value === "personal" || value === "organization"
const credentialLifetime = (value: string): value is "persistent" | "session" => value === "persistent" || value === "session"
export function productCredentialBody(draft: CredentialDraft, existing: CredentialSecretMetadata, clientId: string): ProductUpdateSecretWritable
export function productCredentialBody(draft: CredentialDraft, existing: undefined, clientId: string): ProductSaveSecretWritable
export function productCredentialBody(draft: CredentialDraft, existing: CredentialSecretMetadata | undefined, clientId: string): ProductUpdateSecretWritable | ProductSaveSecretWritable {
  const { scope, lifetime } = draft
  if (!credentialScope(scope)) throw new Error("Choose who can use this credential.")
  if (!credentialLifetime(lifetime)) throw new Error("Choose this session or future sessions.")
  if (draft.lifetime === "session" && !/^(?:[0-9a-f]{32}|native:[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12})$/.test(draft.root_id)) throw new Error("Choose a session for session-only access.")
  if (!draft.label.trim() || draft.label.trim().length > 80) throw new Error("Use a credential name between 1 and 80 characters.")
  const fields = { label: draft.label.trim(), scope, lifetime, root_id: lifetime === "session" ? draft.root_id : "", expires_at: draft.expires_at }
  if (existing) {
    if (existing.format === "aws-sso") throw new Error("Renew AWS SSO access through its sign-in flow.")
    return { revision: existing.revision, ...fields, ...(draft.value ? { value: draft.value } : {}) }
  }
  if (!draft.value) throw new Error("Provide the credential value.")
  return { provider: draft.provider, ...fields, value: draft.value, client_id: clientId, ...(draft.provider === "generic" ? { name: draft.name.trim(), format: draft.format, env_var: draft.format === "file" ? draft.env_var.trim() : "" } : {}) }
}
function ProductSecretsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/credentials", signal => listCredentials({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [editor, setEditor] = useState<{ secret?: CredentialSecretMetadata; onePassword?: boolean } | null>(null), [revoke, setRevoke] = useState<CredentialSecretMetadata | null>(null)
  return <LoadState resource={resource}>{data => {
    const rows = data.secrets.filter(secret => contains(search, secret.label, secret.name, secret.provider) && (scope === "all" || (secret.scope === "organization" ? "organization" : "personal") === scope))
    return <>
      {!data.capabilities && <p className="ns-notice">You can view saved credentials, but editing is unavailable.</p>}
      {data.capabilities?.native_execution === false && <p className="ns-notice">Saved credentials are currently unavailable to tasks.</p>}
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Credential scope" value={scope} onChange={setScope} options={[{ value: "all", label: "All scopes" }, { value: "personal", label: "Personal" }, { value: "organization", label: "Organization" }]} />}
        action={data.capabilities?.create && <><Button variant="outline" onClick={() => {
          const saved = data.secrets.filter(secret => secret.provider === "generic" && secret.name === "1password-shared" && secret.format === "env")
          if (saved.length === 1 && saved[0].can_manage) setEditor({ secret: saved[0], onePassword: true })
          else if (!saved.length) setEditor({ onePassword: true })
          else { setSearch("1password-shared"); setScope("all") }
        }}>Connect 1Password</Button><Button onClick={() => setEditor({})}><Plus />Add credential</Button></>} />
      {rows.length ? <ProductSecretRows secrets={rows} providers={data.providers} capabilities={data.capabilities} onEdit={secret => setEditor({ secret, onePassword: secret.name === "1password-shared" && secret.format === "env" })} onRevoke={setRevoke} /> : <Empty filtered={Boolean(search) || scope !== "all"} noun="saved credentials" onClear={() => { setSearch(""); setScope("all") }} />}
      {editor && (editor.secret ? data.capabilities?.update && editor.secret.can_manage : data.capabilities?.create) && <ProductCredentialEditor key={editor.secret?.id || (editor.onePassword ? "1password" : "new")} user={user} inventory={data} existing={editor.secret} onePassword={editor.onePassword} onClose={() => setEditor(null)} onSaved={resource.refresh} />}
      {revoke && data.capabilities?.delete && revoke.can_manage && <Confirm title={`Revoke ${revoke.label}?`} description="Future tasks cannot use this saved access. The credential is not revoked at its provider, and existing conversations are retained." label="Revoke access"
        onConfirm={async () => { await revokeCredential({ path: { identity: revoke.id } }); resource.refresh() }} onClose={() => setRevoke(null)} />}
    </>
  }}</LoadState>
}

function ProductCredentialEditor({ user, inventory, existing, onePassword, onClose, onSaved }: {
  user: SettingsUser; inventory: CredentialInventory; existing?: CredentialSecretMetadata; onePassword?: boolean; onClose: () => void; onSaved: () => void
}) {
  const mutation = useMutation(), id = useId(), clientId = useRef(crypto.randomUUID())
  const opener = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  const [draft, setDraft] = useState<CredentialDraft>({
    provider: existing ? inventory.providers.find(item => item.id === existing.provider)?.id ?? "generic" : onePassword ? "generic" : inventory.providers[0]?.id ?? "generic",
    label: existing?.label || (onePassword ? "1Password Shared" : ""), name: existing?.name || (onePassword ? "1password-shared" : ""),
    format: existing?.format === "file" ? "file" : "env", env_var: existing?.env_var || "", scope: existing ? existing.scope === "session" ? "personal" : existing.scope : "",
    lifetime: existing?.lifetime || (existing ? existing.scope === "session" ? "session" : "persistent" : ""), root_id: existing?.root_id || "", expires_at: existing?.expires_at || "", value: "",
  })
  const [expiry, setExpiry] = useState(onceUtcInput(existing?.expires_at || null))
  const [variables, setVariables] = useState<{ key: string; value: string; id: string }[]>([{ key: onePassword ? "OP_SERVICE_ACCOUNT_TOKEN" : "", value: "", id: crypto.randomUUID() }])
  const file = useRef<HTMLInputElement>(null)
  const [cursor, setCursor] = useState<string | null>(null), [savedRoots, setSavedRoots] = useState<CredentialRootSession[]>([])
  const roots = useHistoryRoots(draft.lifetime === "session", cursor)
  useEffect(() => { if (roots.error) setSavedRoots([]) }, [roots.error])
  const rootRows = roots.error ? [] : [...new Map([...savedRoots, ...roots.data?.sessions ?? []].map(row => [`${row.source}:${row.id}`, row])).values()]
  const rootOptions = rootRows.filter(row => !row.parent_session_id && !row.deleted && !row.deleting).flatMap(row => {
    try { return [{ value: productCredentialRoot(row), label: `${row.title} · ${row.source === "legacy" ? "Saved history" : "Current session"}` }] } catch { return [] }
  })
  if (existing?.root_id && !rootOptions.some(option => option.value === existing.root_id)) rootOptions.unshift({ value: existing.root_id, label: "Previously saved session" })
  const change = (patch: Partial<CredentialDraft>) => setDraft(previous => ({ ...previous, ...patch }))
  const generic = draft.provider === "generic", supportedScopes = inventory.capabilities?.scopes ?? []
  const provider = inventory.providers.find(item => item.id === draft.provider)
  let setup: string | undefined
  try { if (provider?.setup_url?.startsWith("https://")) setup = productRedirect(provider.setup_url, location.origin) } catch {}
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); if (opener.current?.isConnected) opener.current.focus() }}>
    <DialogHeader><DialogTitle>{existing ? "Edit saved access" : onePassword ? "Connect 1Password Shared" : "Save a credential"}</DialogTitle><DialogDescription>Choose who can use this credential and how long it stays available.</DialogDescription></DialogHeader>
    <form className="ns-fields" onSubmit={event => { event.preventDefault(); void mutation.run(async () => {
      let value = draft.value
      if (generic && draft.format === "env") {
        const used = variables.filter(variable => variable.key || variable.value)
        if (used.some(variable => variable.value)) {
          if (used.some(variable => !/^[A-Z_][A-Z0-9_]{0,127}$/.test(variable.key) || !variable.value)) throw new Error("Give every environment variable a valid name and value.")
          if (new Set(used.map(variable => variable.key)).size !== used.length) throw new Error("Use each environment variable name only once.")
          value = JSON.stringify(Object.fromEntries(used.map(variable => [variable.key, variable.value])))
        } else value = ""
      }
      if (generic && draft.format === "file") {
        const selected = file.current?.files?.[0]
        if (selected && selected.size > 131072) throw new Error("Choose a UTF-8 credential file of at most 128 KB.")
        value = selected ? await selected.text() : ""
        if (value.includes("\0")) throw new Error("Choose a UTF-8 text credential file without NUL characters.")
      }
      const saved = { ...draft, value, expires_at: expiry ? onceUtcInstant(expiry) : "" }
      const save = existing
        ? (body => () => updateCredential({ path: { identity: existing.id }, body }))(productCredentialBody(saved, existing, clientId.current))
        : (body => () => createCredential({ body }))(productCredentialBody(saved, undefined, clientId.current))
      if (!supportedScopes.includes(draft.scope) || (draft.scope === "organization" && user.role !== "admin")) throw new Error("This sign-in cannot save credentials in the selected scope.")
      if (!inventory.capabilities?.lifetimes.includes(draft.lifetime)) throw new Error("This credential lifetime is not supported.")
      await save()
    }, () => { setDraft(previous => ({ ...previous, value: "" })); setVariables([]); if (file.current) file.current.value = ""; onSaved(); onClose() }) }}>
      {onePassword && <p className="ns-help">Use a 1Password service account with access to the Shared vault. Choose Organization to share this access with teammates.</p>}
      <div className="ns-field"><label htmlFor={`${id}-provider`}>Service</label><Choice id={`${id}-provider`} label="Credential service" value={draft.provider} disabled={mutation.busy || Boolean(existing) || Boolean(onePassword)} options={inventory.providers.map(item => ({ value: item.id, label: item.name }))} onChange={value => { const selected = inventory.providers.find(item => item.id === value); if (!selected) return; change({ provider: selected.id, value: "", name: "", format: "env", env_var: "" }); setVariables([{ key: "", value: "", id: crypto.randomUUID() }]) }} /></div>
      {setup && <a className="ns-link" href={setup} target="_blank" rel="noreferrer">Provider setup guide</a>}
      <div className="ns-field"><label htmlFor={`${id}-label`}>Display name</label><Input id={`${id}-label`} autoFocus required maxLength={80} disabled={mutation.busy} value={draft.label} onChange={event => change({ label: event.target.value })} /></div>
      {generic && <>
        <div className="ns-field"><label htmlFor={`${id}-name`}>Service or capability name</label><Input id={`${id}-name`} required maxLength={80} disabled={mutation.busy || Boolean(existing) || Boolean(onePassword)} value={draft.name} onChange={event => change({ name: event.target.value })} /></div>
        <div className="ns-field"><label htmlFor={`${id}-format`}>Credential format</label><Choice id={`${id}-format`} label="Credential format" value={draft.format} disabled={mutation.busy || Boolean(existing) || Boolean(onePassword)} onChange={value => { change({ format: value as "env" | "file", value: "" }); setVariables([{ key: "", value: "", id: crypto.randomUUID() }]) }} options={[{ value: "env", label: "Environment variables" }, { value: "file", label: "Credential file" }]} /></div>
        {draft.format === "file" ? <>
          <div className="ns-field"><label htmlFor={`${id}-envvar`}>Environment variable for the file path</label><Input id={`${id}-envvar`} required maxLength={128} pattern="[A-Z_][A-Z0-9_]*" disabled={mutation.busy || Boolean(existing)} value={draft.env_var} onChange={event => change({ env_var: event.target.value })} /></div>
          <div className="ns-field"><label htmlFor={`${id}-file`}>{existing ? "Replacement credential file" : "Credential file"}</label><Input id={`${id}-file`} ref={file} type="file" required={!existing} disabled={mutation.busy} /><p className="ns-field-hint">UTF-8 text, up to 128 KB. Its contents are never displayed here.</p></div>
        </> : <fieldset className="ns-fields"><legend>{existing ? "Replacement environment variables" : "Environment variables"}</legend>
          {variables.map((variable, index) => <div className="ns-credential-variable" key={variable.id}>
            <div className="ns-field"><label htmlFor={`${id}-key-${index}`}>Variable name</label><Input id={`${id}-key-${index}`} value={variable.key} maxLength={128} autoComplete="off" disabled={mutation.busy || Boolean(onePassword)} onChange={event => setVariables(previous => previous.map(item => item.id === variable.id ? { ...item, key: event.target.value } : item))} /></div>
            <div className="ns-field"><label htmlFor={`${id}-value-${index}`}>{onePassword ? "Service account token" : "Secret value"}</label><Input id={`${id}-value-${index}`} type="password" autoComplete="off" value={variable.value} disabled={mutation.busy} onChange={event => setVariables(previous => previous.map(item => item.id === variable.id ? { ...item, value: event.target.value } : item))} /></div>
            {!onePassword && <Button type="button" variant="ghost" size="icon-sm" disabled={mutation.busy || variables.length === 1} aria-label={`Remove variable ${index + 1}`} onClick={() => setVariables(previous => previous.filter(item => item.id !== variable.id))}><Trash2 size={16} /></Button>}
          </div>)}
          {!onePassword && <Button type="button" variant="outline" disabled={mutation.busy} onClick={() => setVariables(previous => [...previous, { key: "", value: "", id: crypto.randomUUID() }])}>Add environment variable</Button>}
        </fieldset>}
      </>}
      {!generic && <div className="ns-field"><label htmlFor={`${id}-token`}>{existing ? "Replacement API key" : "API key"}</label><Input id={`${id}-token`} type="password" autoComplete="off" required={!existing} disabled={mutation.busy} value={draft.value} onChange={event => change({ value: event.target.value })} /></div>}
      {existing && <p className="ns-field-hint">Leave replacement values blank to keep the saved value. A supplied environment map replaces the complete saved map.</p>}
      <div className="ns-field"><label htmlFor={`${id}-scope`}>Who can use this credential?</label><Choice id={`${id}-scope`} label="Credential sharing" value={draft.scope} disabled={mutation.busy} options={supportedScopes.filter(scope => scope !== "organization" || user.role === "admin").map(scope => ({ value: scope, label: scope === "organization" ? "Organization · Everyone" : "Personal · My requests" }))} onChange={value => change({ scope: value })} /><p className="ns-field-hint">Personal credentials work in your private web chats. Shared chats and Slack tasks need organization credentials.</p></div>
      {!supportedScopes.includes("personal") && <p className="ns-field-hint">Sign in with your individual work account to save personal credentials.</p>}
      <div className="ns-field"><label htmlFor={`${id}-lifetime`}>When can it be used?</label><Choice id={`${id}-lifetime`} label="Credential lifetime" value={draft.lifetime} disabled={mutation.busy} options={(inventory.capabilities?.lifetimes || []).map(value => ({ value, label: value === "session" ? "This session only" : "Across future sessions" }))} onChange={value => change({ lifetime: value })} /></div>
      {draft.lifetime === "session" && <div className="ns-field"><label htmlFor={`${id}-root`}>Session</label><Choice id={`${id}-root`} label="Credential session" value={draft.root_id} disabled={mutation.busy || roots.loading} options={rootOptions} onChange={value => change({ root_id: value })} /><p className="ns-field-hint">Choose the chat that needs this credential.</p>
        {roots.loading && <p role="status">Loading sessions…</p>}{roots.error && <p className="ns-error" role="alert">{roots.error}<Button type="button" variant="outline" onClick={roots.refresh}>Retry sessions</Button></p>}
        {roots.data?.next_cursor && <Button type="button" variant="outline" disabled={roots.loading || mutation.busy} onClick={() => { setSavedRoots(rootRows); setCursor(roots.data!.next_cursor!) }}>Load more sessions</Button>}
      </div>}
      <div className="ns-field"><label htmlFor={`${id}-expiry`}>Provider expiry (optional, UTC)</label><Input id={`${id}-expiry`} type="datetime-local" step={1} disabled={mutation.busy} value={expiry} onChange={event => setExpiry(event.target.value)} /><p className="ns-field-hint">Leave blank if the provider expiry is unknown. Session-only access does not change the provider’s expiry.</p></div>
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={mutation.busy} onClick={onClose}>Cancel</Button><Button type="submit" disabled={mutation.busy}>{mutation.busy ? "Saving…" : "Save credential"}</Button></div>
    </form>
  </DialogContent></Dialog>
}

function MemoryPage() {
  const [namespace, setNamespace] = useState("")
  const resource = useResource(`/settings/memory${namespace ? `?${new URLSearchParams({ namespace })}` : ""}`, signal => listSettingsMemory({ query: namespace ? { namespace } : undefined, signal }).then(({ data }) => data))
  const [options, setOptions] = useState<Option[]>([])
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<SettingsMemoryFile | "new" | null>(null), [remove, setRemove] = useState<SettingsMemoryFile | null>(null)
  useEffect(() => { if (resource.data) setOptions(resource.data.namespaces.map(item => ({ value: item.id, label: item.label }))) }, [resource.data])
  return <>
    {options.length > 0 && <div className="ns-scope-picker"><label htmlFor="memory-namespace">Memory namespace</label><Choice id="memory-namespace" label="Memory namespace" value={namespace || resource.data?.namespace || resource.data?.default_namespace || options[0].value} onChange={value => { setNamespace(value); setEditor(null); setRemove(null) }} options={options} /></div>}
    <LoadState resource={resource}>{data => {
      const rows = data.memories.filter(row => contains(search, row.path, row.content))
      return <><CapabilityNote {...data} />{data.truncated && <p className="ns-notice">Showing the first {count(data.memories.length)} memory files in this namespace. Additional files are not included in this list.</p>}<Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
        action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add memory</Button>} />
        {rows.length ? <div className="ns-records">{rows.map(row => <article key={`${row.namespace}/${row.path}`} className="ns-record">
          <div className="ns-record-copy"><h2>{row.path}</h2><details><summary>View memory</summary><pre>{row.content}</pre></details>{row.truncated && <p>This preview is truncated. Editing is unavailable to avoid overwriting unseen content.</p>}</div>
          <div className="ns-row-actions">{data.capabilities?.update && !row.truncated && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.path}</span></Button>}
            {data.capabilities?.delete && <Button size="icon-sm" variant="ghost" aria-label={`Delete ${row.path}`} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
        </article>)}</div> : <Empty filtered={Boolean(search)} noun="memories" onClear={() => setSearch("")} />}
        {editor && <Editor title={editor === "new" ? "Add memory" : "Edit memory"} description="Changes apply to the selected namespace. A newer saved version will not be overwritten." initial={{ path: editor === "new" ? "" : editor.path, content: editor === "new" ? "" : editor.content }} fields={[
          { name: "path", label: "Path", required: true, disabled: editor !== "new", pattern: "(?!.*\\.\\.)[A-Za-z0-9_][A-Za-z0-9_.\\-]{0,199}\\.md", maxLength: 203, hint: "Use a Markdown filename such as preferences.md, without directory segments." },
          { name: "content", label: "Memory content", type: "textarea", required: true, maxLength: 65536 },
        ]} onSave={async values => { await writeSettingsMemory({ query: { namespace: editor === "new" ? data.namespace : editor.namespace, path: String(values.path) }, body: { content: String(values.content), version: editor === "new" ? null : editor.version } }); resource.refresh() }} onClose={() => setEditor(null)} />}
        {remove && <Confirm title={`Delete ${remove.path}?`} description="This removes the saved memory from this namespace. Existing conversations are kept." label="Delete memory" onConfirm={async () => { await deleteSettingsMemory({ query: { namespace: remove.namespace, path: remove.path, ...(remove.version ? { version: remove.version } : {}) } }); resource.refresh() }} onClose={() => setRemove(null)} />}
      </>
    }}</LoadState>
  </>
}

const productMemoryKinds: { value: ProductMemoryNote["kind"]; label: string }[] = [{ value: "preference", label: "Preference" }, { value: "feedback", label: "Correction" }, { value: "project", label: "Project context" }, { value: "reference", label: "Reference" }]
export function productMemorySourceLink(source: ProductMemoryNote["source"]): string | undefined {
  if (!source?.session_id || !source.url) return
  const hash = source.engine === "legacy" && /^[0-9a-f]{32}$/.test(source.session_id) ? `#run=${source.session_id}`
    : source.engine === "native" && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(source.session_id) ? `#${source.session_id}` : undefined
  return hash && (source.url === hash || source.url === `/${hash}`) ? hash : undefined
}
/** Render canonical personal notes without flattening them into Harness filenames. */
export function ProductMemoryNotes({ notes, capabilities, onEdit, onRemove }: {
  notes: ProductMemoryNote[]; capabilities?: Capabilities; onEdit: (note: ProductMemoryNote) => void; onRemove: (note: ProductMemoryNote) => void
}) {
  return <div className="ns-records">{notes.map(note => {
    const expires = note.expires_at ? Date.parse(note.expires_at) : NaN
    const sourceLink = productMemorySourceLink(note.source)
    return <article className="ns-record" key={note.id}><div className="ns-record-copy">
      <div className="ns-record-title"><h2>{note.title}</h2><span className="ns-badge">{productMemoryKinds.find(kind => kind.value === note.kind)?.label ?? note.kind}</span>
        {Number.isFinite(expires) && expires <= Date.now() && <span className="ns-badge">Expired</span>}</div>
      {note.review_required && <p className="ns-notice">This note’s source needs review. It is excluded from automatic recall. Review and save it to confirm the content, or delete it.</p>}
      <details><summary>View memory</summary><pre>{note.content}</pre></details>
      <p>{note.repo_url || "Across your sessions"}</p><small>Updated {dateTime(note.updated_at)}{note.expires_at ? ` · Expires ${dateTime(note.expires_at)}` : ""}</small>
      {(note.source?.quote || note.source?.evidence || sourceLink) && <details><summary>Why this was saved</summary>{(note.source?.quote || note.source?.evidence) && <pre>{note.source.quote || note.source.evidence}</pre>}{note.source?.scope && <p>{note.source.scope}</p>}{sourceLink && <a className="ns-link" href={sourceLink}>Open original session</a>}</details>}
      {note.truncated && <p>The preview is incomplete. Editing is unavailable to protect unseen content.</p>}
    </div><div className="ns-row-actions">
      {capabilities?.update && !note.truncated && <Button variant="outline" size="sm" onClick={() => onEdit(note)}>Review & edit<span className="visually-hidden"> {note.title}</span></Button>}
      {capabilities?.delete && <Button variant="ghost" size="sm" onClick={() => onRemove(note)}>Delete<span className="visually-hidden"> {note.title}</span></Button>}
    </div></article>
  })}</div>
}

export function productMemoryWrite(note: ProductMemoryNote | undefined, values: FormValues, requestId: string): ProductMemoryWriteBody {
  const kind = productMemoryKinds.find(item => item.value === values.kind)?.value
  if (!kind) throw new Error("Choose a memory type.")
  const normalizeRepository = (value: string) => value.trim().replace(/\/$/, "").replace(/\.git$/, "").toLowerCase()
  const sameRepository = note && normalizeRepository(note.repo_url) === normalizeRepository(String(values.repo_url || ""))
  return {
    key: note?.key || `note-${requestId.toLowerCase()}`, title: String(values.title), content: String(values.content),
    kind, repo_url: String(values.repo_url || ""), revision: note?.revision ?? 0, request_id: requestId,
    ...(note?.repository_id && sameRepository ? { repository_id: note.repository_id } : {}),
  }
}
function ProductMemoryPage() {
  const resource = useResource("/product/memory", signal => listProductMemories({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState("")
  const [editor, setEditor] = useState<{ note?: ProductMemoryNote; requestId: string } | null>(null)
  const [remove, setRemove] = useState<ProductMemoryNote | null>(null)
  const mutation = useMutation()
  const request = async (call: () => Promise<unknown>) => {
    try { return await call() }
    catch (reason) {
      if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) { setEditor(null); setRemove(null); resource.refresh() }
      throw reason
    }
  }
  return <LoadState resource={resource}>{data => {
    if (data.source !== "canonical") return <p className="ns-error" role="alert">Your saved memories could not be loaded. Refresh to try again.</p>
    if (data.capabilities.read !== true) return <div className="ns-empty"><Shield /><h2>Personal memory is unavailable for this sign-in</h2><p>{data.note || "Use an individual sign-in to view your personal memory."}</p><Button variant="outline" onClick={resource.refresh}>Check access again</Button></div>
    if (!data.preferences || data.scope?.kind !== "personal") return <p className="ns-error" role="alert">Your memory preferences could not be loaded. Refresh to try again.</p>
    const prefs = data.preferences, rows = data.memories.filter(row => contains(search, row.title, row.content, row.repo_url))
    const unavailable = data.runtime && [
      prefs.enabled && !data.runtime.recall && "Using saved memories",
      prefs.enabled && prefs.auto_save && !data.runtime.auto_save && "Automatic saving",
      prefs.enabled && data.review?.enabled && !data.runtime.background_review && "Background review",
    ].filter(Boolean)
    return <>
      {prefs.enabled && !data.runtime ? <p className="ns-notice" role="status">Memory availability could not be checked. Your saved notes are shown below.</p>
        : unavailable && unavailable.length > 0 && <p className="ns-notice" role="status">Currently unavailable: {unavailable.join(", ")}. Your saved notes are kept.</p>}
      <section className="ns-record"><div className="ns-record-copy"><h2>Memory preferences</h2><p>{!prefs.enabled ? "Memory is paused. Your saved notes are kept." : data.runtime?.recall ? "Moyai can use these notes in future chats." : "Choose how memories are saved."}</p>
        {data.review?.latest && <small>Last review: {data.review.latest.status} · {dateTime(data.review.latest.updated_at)}</small>}
      </div><div className="ns-memory-controls">
        <Choice label="How new memories are saved" value={prefs.auto_save ? "auto" : "manual"} disabled={!data.capabilities.preferences || mutation.busy || !prefs.enabled}
          options={[{ value: "auto", label: "Save automatically" }, { value: "manual", label: "Save manually" }]}
          onChange={value => void mutation.run(() => request(() => updateProductMemoryPreferences({ body: { enabled: prefs.enabled, auto_save: value === "auto", revision: prefs.revision } })), resource.refresh)} />
        {data.capabilities.preferences && <Button variant="outline" disabled={mutation.busy} onClick={() => void mutation.run(() => request(() => updateProductMemoryPreferences({ body: { enabled: !prefs.enabled, auto_save: prefs.auto_save, revision: prefs.revision } })), resource.refresh)}>{prefs.enabled ? "Pause memory" : "Resume memory"}</Button>}</div></section>
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} action={data.capabilities.create && <Button onClick={() => setEditor({ requestId: crypto.randomUUID() })}><Plus />Add memory</Button>} />
      {rows.length ? <ProductMemoryNotes notes={rows} capabilities={data.capabilities} onEdit={note => setEditor({ note, requestId: crypto.randomUUID() })} onRemove={setRemove} /> : <Empty filtered={Boolean(search)} noun="memories" onClear={() => setSearch("")} />}
      {editor && (editor.note ? data.capabilities.update && !editor.note.truncated : data.capabilities.create) && <Editor title={editor.note ? "Edit memory" : "Add memory"} description="Keep one useful idea per note."
        initial={{ title: editor.note?.title ?? "", content: editor.note?.content ?? "", kind: editor.note?.kind ?? "preference", repo_url: editor.note?.repo_url ?? "" }}
        fields={[{ name: "title", label: "Title", required: true, minLength: 3, maxLength: 120 }, { name: "kind", label: "Type", type: "select", options: productMemoryKinds },
          { name: "content", label: "What should Moyai remember?", type: "textarea", required: true, minLength: 3 },
          { name: "repo_url", label: "Repository (optional)", type: "url", maxLength: 240, hint: "Use a GitHub repository URL, or leave blank for context across your sessions. Editing project context or a reference refreshes its expiry." }]}
        onSave={async values => {
          const body = productMemoryWrite(editor.note, values, editor.requestId), note = editor.note
          await request(() => note ? updateProductMemory({ path: { note_id: note.id }, body }) : createProductMemory({ body })); resource.refresh()
        }} onClose={() => setEditor(null)} />}
      {remove && data.capabilities.delete && <Confirm title={`Delete ${remove.title}?`} description="This stops future recall of the note. Existing conversations and backups are retained." label="Delete memory"
        onConfirm={async () => { await request(() => deleteProductMemory({ path: { note_id: remove.id }, body: { revision: remove.revision } })); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

export function productSkillFileUrl(id: string, path: string, revision: number): string | undefined {
  if (!/^[0-9a-f]{32}$/.test(id) || !path || /[\\\u0000-\u001f\u007f]/.test(path) || !Number.isSafeInteger(revision) || revision < 1) return
  const parts = path.split("/")
  if (parts.some(part => !part || part === "." || part === "..")) return
  return `/api/product/skills/${encodeURIComponent(id)}/files/${parts.map(encodeURIComponent).join("/")}?revision=${revision}`
}
export function ProductSkillRows({ skills, onView }: { skills: ProductSkillSummary[]; onView: (skill: ProductSkillSummary) => void }) {
  return <div className="ns-records">{skills.map(skill => <article className="ns-record" key={skill.id}><FileText className="ns-record-icon" aria-hidden="true" />
    <div className="ns-record-copy"><div className="ns-record-title"><h2>{skill.name}</h2><span className="ns-badge">{skill.scope === "organization" ? "Organization" : "Personal"}</span>{skill.archived && <span className="ns-badge">Archived</span>}</div>
      <p>{skill.description}</p><small>Saved reference: /{skill.reference} · Updated {dateTime(skill.updated_at)}</small></div>
    <Button size="sm" variant="outline" onClick={() => onView(skill)}>View instructions<span className="visually-hidden"> for {skill.name}</span></Button>
  </article>)}</div>
}
function ProductSkillDialog({ skill, onClose }: { skill: ProductSkillSummary; onClose: () => void }) {
  const resource = useResource<ProductSkillDetail>(`/product/skills/${encodeURIComponent(skill.id)}`, signal => getProductSkill({ path: { skill_id: skill.id }, signal }).then(({ data }) => data))
  const opener = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); if (opener.current?.isConnected) opener.current.focus() }}>
    <DialogHeader><DialogTitle>{skill.name}</DialogTitle><DialogDescription>Type / in a chat to choose this skill.</DialogDescription></DialogHeader>
    <LoadState resource={resource}>{detail => <><div className="ns-field"><label>Saved instructions<Textarea readOnly aria-label="Saved skill instructions" value={detail.instructions} rows={12} /></label></div>{detail.files.length > 0 && <section><h3>Supporting files</h3><ul className="ns-supporting-files">{detail.files.map(file => {
      const href = productSkillFileUrl(detail.id, file.path, detail.revision)
      return <li key={file.path}>{href ? <a className="ns-link" href={href} download>{file.path}</a> : <span>{file.path} · Download unavailable</span>}<small> · {file.size.toLocaleString()} bytes</small></li>
    })}</ul></section>}</>}</LoadState>
    <Button variant="outline" onClick={onClose}>Close</Button>
  </DialogContent></Dialog>
}
function ProductSkillsPage() {
  const [archived, setArchived] = useState(false), [search, setSearch] = useState(""), [scope, setScope] = useState("all")
  const [pages, setPages] = useState(1), [revision, setRevision] = useState(0)
  const [state, setState] = useState<{ archived: boolean; data?: ProductSkillLibrary; loading: boolean; error: string }>({ archived, loading: true, error: "" })
  const [selected, setSelected] = useState<ProductSkillSummary | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setState(previous => ({ archived, data: previous.archived === archived ? previous.data : undefined, loading: true, error: "" }))
    void (async () => {
      try {
        let offset = 0, result: ProductSkillLibrary | undefined
        const rows = new Map<string, ProductSkillSummary>()
        for (let page = 0; page < pages; page++) {
          const { data: next } = await listProductSkills({ query: { archived, limit: 100, offset }, signal: controller.signal })
          if (controller.signal.aborted) return
          if (next.source !== "canonical") throw new Error("Your saved skills could not be loaded. Refresh to try again.")
          for (const skill of next.skills) rows.set(skill.id, skill)
          result = { ...next, skills: [...rows.values()] }
          if (!next.has_more || next.next_offset === null || next.next_offset <= offset) break
          offset = next.next_offset
        }
        if (!controller.signal.aborted) setState({ archived, data: result, loading: false, error: "" })
      } catch (reason) { if (!controller.signal.aborted) { setState({ archived, loading: false, error: errorText(reason) }); setSelected(null) } }
    })()
    return () => controller.abort()
  }, [archived, pages, revision])
  const resource: Resource<ProductSkillLibrary> = {
    data: state.archived === archived ? state.data : undefined, loading: state.archived !== archived || state.loading,
    error: state.archived === archived ? state.error : "", refresh: () => setRevision(value => value + 1),
  }
  return <LoadState resource={resource}>{data => {
    if (data.capabilities?.read !== true) return <div className="ns-empty" role="status"><h2>Skills unavailable</h2><p>{data.note || "Your skills could not be loaded."}</p><Button variant="outline" onClick={resource.refresh}>Retry</Button></div>
    const rows = data.skills.filter(row => contains(search, row.name, row.description, row.reference) && (scope === "all" || row.scope === scope))
    return <><p className="ns-help">Type <kbd>/</kbd> in a chat to choose a skill.</p>
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
        filter={<Choice label="Skill scope" value={scope} onChange={setScope} options={[{ value: "all", label: "All scopes" }, { value: "personal", label: "Personal" }, { value: "organization", label: "Organization" }]} />} />
      <label className="check-label"><Checkbox checked={archived} onCheckedChange={value => { setArchived(value === true); setPages(1); setSelected(null) }} />Include archived skills</label>
      {rows.length ? <ProductSkillRows skills={rows} onView={setSelected} /> : <Empty filtered={Boolean(search) || scope !== "all"} noun="skills" onClear={() => { setSearch(""); setScope("all") }} />}
      {data.has_more && <><p className="ns-muted">More saved skills are available. Search filters the loaded records.</p><Button variant="outline" disabled={resource.loading} onClick={() => setPages(value => value + 1)}>Load more skills</Button></>}
      {selected && <ProductSkillDialog skill={selected} onClose={() => setSelected(null)} />}
    </>
  }}</LoadState>
}

const productProviders: Record<string, string> = { github: "GitHub", slack: "Slack", linear: "Linear", notion: "Notion" }
export function productRedirect(value: string, origin: string): string {
  if (typeof value !== "string" || !value) throw new Error("The connection service did not return a destination.")
  const url = new URL(value, origin)
  if (url.username || url.password || /[\\\u0000-\u0020\u007f]/.test(value) || (url.protocol !== "https:" && url.origin !== origin)) throw new Error("The connection service returned an invalid destination.")
  return url.href
}
export function ProductConnectionRows({ connections, admin, onManage }: { connections: OrganizationConnection[]; admin: boolean; onManage: (connection: OrganizationConnection) => void }) {
  return <div className="ns-records">{connections.map(connection => <article className="ns-record" key={connection.id}><Link2 className="ns-record-icon" aria-hidden="true" />
    <div className="ns-record-copy"><div className="ns-record-title"><h2>{productProviders[connection.id] || connection.id}</h2><span className="ns-badge">{connection.connected ? connection.enabled ? "Connected" : "Paused" : "Not connected"}</span><span className="ns-badge">Organization</span></div>
      <p>{connection.label || "No account connected"}</p><small>{connection.identity || "No provider identity"} · {connection.read_only ? "Read-only policy" : "Read and write policy"}</small>
      <small>{connection.check_status === "healthy" ? `Last checked ${dateTime(connection.checked_at)}` : connection.check_status === "needs_attention" ? "Connection needs attention" : "Not checked"}</small>
    </div><Button size="sm" variant="outline" onClick={() => onManage(connection)}>{admin ? "Manage" : "View access"}<span className="visually-hidden"> for {productProviders[connection.id] || connection.id}</span></Button>
  </article>)}</div>
}
function ProductConnectionsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/product/connections", signal => listOrganizationConnections({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [selected, setSelected] = useState<{ connection: OrganizationConnection; mode: "manage" | "connect" | "repositories" } | null>(null)
  const opener = useRef<HTMLElement | null>(null)
  const open = (connection: OrganizationConnection) => { opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null; setSelected({ connection, mode: "manage" }) }
  return <>
    <LoadState resource={resource}>{data => {
      const rows = data.filter(row => contains(search, row.id, row.label, row.identity) && (filter === "all" || row.id === filter))
      return <><p className="ns-help">These accounts are shared with your workspace.</p>
        <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Connected service" value={filter} onChange={setFilter} options={[{ value: "all", label: "All services" }, ...Object.entries(productProviders).map(([value, label]) => ({ value, label }))]} />} />
        {rows.length ? <ProductConnectionRows connections={rows} admin={user.role === "admin"} onManage={open} /> : <Empty filtered={Boolean(search) || filter !== "all"} noun="organization connections" onClear={() => { setSearch(""); setFilter("all") }} />}
        {selected && <ProductConnectionDialog connection={selected.connection} mode={selected.mode} admin={user.role === "admin"} opener={opener}
          onMode={mode => setSelected(current => current ? { ...current, mode } : null)} onClose={() => setSelected(null)}
          onUpdated={() => { resource.refresh(); setSelected(null) }} />}
      </>
    }}</LoadState>
    <p className="ns-help ns-footer-help">Read meeting notes with <a className="ns-link" href="#settings/granola">Granola</a>. To use your own Slack account, connect <a className="ns-link" href="#settings/personal-slack">Personal Slack</a>.</p>
  </>
}
function ProductConnectionDialog({ connection, mode, admin, opener, onMode, onClose, onUpdated }: {
  connection: OrganizationConnection; mode: "manage" | "connect" | "repositories"; admin: boolean
  opener: React.MutableRefObject<HTMLElement | null>; onMode: (mode: "manage" | "connect" | "repositories") => void; onClose: () => void; onUpdated: () => void
}) {
  const mutation = useMutation()
  const [nestedBusy, setNestedBusy] = useState(false)
  const [enabled, setEnabled] = useState(connection.enabled), [readOnly, setReadOnly] = useState(connection.read_only)
  const [disconnect, setDisconnect] = useState(false), [notice, setNotice] = useState("")
  const name = productProviders[connection.id] || connection.id
  return <Dialog open onOpenChange={open => { if (!open && !mutation.busy && !nestedBusy) onClose() }}><DialogContent className="native-settings-dialog"
    onCloseAutoFocus={event => { event.preventDefault(); if (opener.current?.isConnected) opener.current.focus({ preventScroll: true }) }}>
    <DialogHeader><DialogTitle>{name} · {mode === "repositories" ? "Repositories" : mode === "connect" ? "Connect account" : "Organization access"}</DialogTitle>
      <DialogDescription>{mode === "manage" ? "Manage how your workspace can use this account." : "Connect an account for your workspace."}</DialogDescription></DialogHeader>
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}{notice && <p className="ns-notice" role="status">{notice}</p>}
    {mode === "manage" && <>
      <p>{connection.connected ? `${connection.label} · ${connection.identity}` : "No organization account is connected."}</p>
      {connection.tools.length > 0 && <details><summary>Available actions</summary><ul className="ns-supporting-files">{connection.tools.map((tool, index) => <li key={tool.name || index}>{tool.name && <code>{tool.name} · </code>}{tool.description}</li>)}</ul></details>}
      {admin ? <form onSubmit={event => { event.preventDefault(); void mutation.run(() => updateOrganizationConnectionPolicy({ path: { provider: connection.id }, body: { enabled, read_only: readOnly } }), onUpdated) }}>
        <div className="ns-fields"><label className="check-label"><Checkbox checked={enabled} disabled={mutation.busy} onCheckedChange={value => setEnabled(value === true)} />Available to organization sessions</label>
          <div className="ns-field"><label>Allowed actions<Choice label="Allowed connection actions" value={readOnly ? "read" : "write"} onChange={value => setReadOnly(value === "read")} disabled={mutation.busy} options={[{ value: "read", label: "Read only" }, { value: "write", label: "Read and write" }]} /></label></div>
          <p className="ns-field-hint">Pausing access or changing it to read only blocks new writes. Actions already sent cannot be recalled.</p></div>
        <div className="ns-dialog-actions"><Button type="submit" disabled={mutation.busy}>{mutation.busy ? "Saving…" : "Save access"}</Button></div>
      </form> : <p>{connection.enabled ? "Available to sessions." : "Paused for organization sessions."} Ask an administrator to change this policy.</p>}
      {admin && <div className="ns-row-actions">
        <Button variant="outline" disabled={mutation.busy} onClick={() => onMode("connect")}>{connection.connected ? "Reconnect" : "Connect"} {name}</Button>
        {connection.connected && <Button variant="outline" disabled={mutation.busy} onClick={() => void mutation.run(() => checkOrganizationConnection({ path: { provider: connection.id } }), () => setNotice("The provider confirmed this connection."))}>Check connection</Button>}
        {connection.id === "github" && connection.connected && <Button variant="outline" disabled={mutation.busy} onClick={() => onMode("repositories")}>Choose repositories</Button>}
        {connection.connected && <Button variant="ghost" disabled={mutation.busy} onClick={() => setDisconnect(true)}>Disconnect…</Button>}
      </div>}
      {disconnect && admin && <div className="ns-empty"><h3>Disconnect {name} for the organization?</h3><p>Future tasks cannot use this account. Actions already sent and provider credentials are not recalled.</p><div className="ns-dialog-actions">
        <Button autoFocus variant="outline" disabled={mutation.busy} onClick={() => setDisconnect(false)}>Cancel</Button>
        <Button variant="destructive" disabled={mutation.busy} onClick={() => void mutation.run(() => disconnectOrganizationConnection({ path: { provider: connection.id } }), onUpdated)}>Disconnect for everyone</Button>
      </div></div>}
    </>}
    {mode === "connect" && admin && <ProductConnectionSetup connection={connection} onDone={onUpdated} onBack={() => onMode("manage")} onBusyChange={setNestedBusy} />}
    {mode === "repositories" && admin && connection.id === "github" && <ProductRepositorySelection onDone={onUpdated} onBack={() => onMode("manage")} onBusyChange={setNestedBusy} />}
    <Button variant="outline" disabled={mutation.busy || nestedBusy} onClick={onClose}>Close</Button>
  </DialogContent></Dialog>
}
function ProductConnectionSetup({ connection, onDone, onBack, onBusyChange }: { connection: OrganizationConnection; onDone: () => void; onBack: () => void; onBusyChange: (busy: boolean) => void }) {
  const mutation = useMutation()
  const [token, setToken] = useState(""), [organization, setOrganization] = useState(""), [appId, setAppId] = useState("")
  const file = useRef<HTMLInputElement>(null)
  useEffect(() => { onBusyChange(mutation.busy); return () => onBusyChange(false) }, [mutation.busy, onBusyChange])
  const name = productProviders[connection.id] || connection.id
  const redirect = (start: () => Promise<{ url?: string; connected?: boolean }>) => {
    let result: { url?: string; connected?: boolean }
    return mutation.run(async () => { result = await start() }, () => {
      if (result.connected) onDone()
      else if (result.url) window.location.assign(productRedirect(result.url, location.origin))
    })
  }
  return <div className="ns-fields">
    {connection.id === "github" ? <>
      <p>Connect the organization’s GitHub App. Its installation controls which repositories can be selected.</p>
      {connection.app_registered ? <Button disabled={mutation.busy} onClick={() => void redirect(() => startGitHubConnection().then(({ data }) => data))}>Continue with GitHub</Button> : <>
        <form className="ns-fields" onSubmit={event => { event.preventDefault(); let destination: string; void mutation.run(async () => {
          const selected = file.current?.files?.[0]
          if (!selected || selected.size > 20_000) throw new Error("Choose a PEM private key file under 20 KB.")
          const { data: result } = await connectExistingGitHubApp({ body: { app_id: Number(appId), private_key: await selected.text() } })
          if (file.current) file.current.value = ""
          destination = productRedirect(result.url, location.origin)
        }, () => window.location.assign(destination)) }}>
          <h3>Use an existing GitHub App</h3>
          <div className="ns-field"><label>GitHub App ID<Input type="number" min={1} required disabled={mutation.busy} value={appId} onChange={event => setAppId(event.target.value)} /></label></div>
          <div className="ns-field"><label>PEM private key<Input ref={file} type="file" accept=".pem" required disabled={mutation.busy} /></label><p className="ns-field-hint">Choose the key from GitHub App settings. It is never displayed here or placed in chat.</p></div>
          <Button type="submit" disabled={mutation.busy}>Verify app and continue</Button>
        </form>
        <form className="ns-fields" onSubmit={event => { event.preventDefault(); void redirect(() => startGitHubConnection({ body: { organization: organization.trim() } }).then(({ data }) => data)) }}>
          <h3>Register an organization App</h3><div className="ns-field"><label>GitHub organization<Input required maxLength={39} pattern="[A-Za-z0-9][A-Za-z0-9-]{0,38}" disabled={mutation.busy} value={organization} onChange={event => setOrganization(event.target.value)} /></label></div>
          <Button type="submit" variant="outline" disabled={mutation.busy}>Continue to GitHub registration</Button>
        </form>
      </>}
    </> : <>
      {connection.oauth_configured ? <Button disabled={mutation.busy} onClick={() => void redirect(() => startOrganizationConnectionOAuth({ path: { provider: connection.id } }).then(({ data }) => data))}>Continue with {name}</Button>
        : <p>OAuth is not configured for this provider. An administrator can connect using a supported access token.</p>}
      <form className="ns-fields" onSubmit={event => { event.preventDefault(); void mutation.run(() => connectOrganizationConnection({ path: { provider: connection.id }, body: { token } }), () => { setToken(""); onDone() }) }}>
        <div className="ns-field"><label>Access token<Input type="password" autoComplete="off" required minLength={1} maxLength={100000} disabled={mutation.busy} value={token} onChange={event => setToken(event.target.value)} /></label><p className="ns-field-hint">The provider validates the token. It is stored encrypted and is never returned to this page.</p></div>
        <Button type="submit" disabled={mutation.busy}>Connect {name} using token</Button>
      </form>
    </>}
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
    <Button variant="outline" disabled={mutation.busy} onClick={onBack}>Back to account</Button>
  </div>
}
function ProductRepositorySelection({ onDone, onBack, onBusyChange }: { onDone: () => void; onBack: () => void; onBusyChange: (busy: boolean) => void }) {
  const resource = useResource("/connections/github/repositories", signal => listGitHubRepositories({ signal }).then(({ data }) => data))
  const mutation = useMutation(), initialized = useRef(false)
  const [selected, setSelected] = useState<number[]>([]), [search, setSearch] = useState("")
  useEffect(() => { onBusyChange(mutation.busy); return () => onBusyChange(false) }, [mutation.busy, onBusyChange])
  useEffect(() => { if (resource.data && !initialized.current) { initialized.current = true; setSelected(resource.data.selected_ids) } }, [resource.data])
  return <LoadState resource={resource}>{data => {
    const rows = data.repositories.filter(row => contains(search, row.full_name))
    let installation: string | undefined
    try { installation = productRedirect(data.installation_url, location.origin) } catch {}
    return <form onSubmit={event => { event.preventDefault(); void mutation.run(() => selectGitHubRepositories({ body: { repository_ids: selected } }), onDone) }}>
      <p>Choose which repositories Moyai can access.</p>
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} />
      <div className="ns-fields">{rows.map(repo => <label className="check-label" key={repo.id}><Checkbox checked={selected.includes(repo.id)} disabled={mutation.busy} onCheckedChange={checked => setSelected(previous => checked ? [...new Set([...previous, repo.id])] : previous.filter(id => id !== repo.id))} />{repo.full_name}</label>)}</div>
      {installation && <p className="ns-muted"><a className="ns-link" href={installation} target="_blank" rel="noreferrer">Manage the GitHub App’s repository access</a>, then refresh this list.</p>}
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={mutation.busy} onClick={onBack}>Back</Button><Button type="submit" disabled={mutation.busy || !selected.length}>Save repositories</Button></div>
    </form>
  }}</LoadState>
}
function ProductPersonalSlack({ individual, onStartPrivateChat }: { individual: boolean; onStartPrivateChat: () => void }) {
  const resource = useResource("/connections/slack/personal", signal => getPersonalSlack({ signal }).then(({ data }) => data))
  const mutation = useMutation(), [disconnect, setDisconnect] = useState(false)
  const [checked, setChecked] = useState(false)
  return <LoadState resource={resource}>{data => {
    const healthy = data.connected && data.health === "healthy"
    const canStart = data.available && individual
    const connect = () => {
      let url: string
      void mutation.run(async () => { url = (await startPersonalSlackOAuth()).data.url }, () => window.location.assign(productRedirect(url, location.origin)))
    }
    return <section className="ns-personal-slack" aria-label="Your Slack connection">
      <div className="ns-connection-card">
        <div className="ns-connection-heading">
          <span className="ns-provider-icon"><Slack size={24} aria-hidden="true" /></span>
          <div className="ns-connection-identity"><h2>{data.connected ? data.label || "Your Slack account" : "Connect Slack"}</h2>
            <span className={`ns-connection-status ${healthy ? "is-connected" : ""}`}><span aria-hidden="true" />{data.connected ? healthy ? "Connected" : "Needs attention" : "Not connected"}</span>
          </div>
        </div>
        <p className="ns-help">Available in your private web chats. Not used in shared chats, Slack threads, or automations.</p>
        {data.connected && !healthy && <p className="ns-notice" role="status">Reconnect to restore access. Moyai will not switch to a workspace account if this connection fails.</p>}
        {!data.connected && data.effective_source === "organization" && <p className="ns-help">Your chats currently use the workspace Slack connection.</p>}
        {!data.available && <p className="ns-notice" role="status">Sign in with your individual work account to connect Slack.</p>}
        {data.available && !data.oauth_configured && <p className="ns-notice" role="status">Ask a workspace administrator to enable Slack sign-in.</p>}
        <div className="ns-row-actions">
          {healthy && canStart && <Button disabled={mutation.busy} onClick={onStartPrivateChat}>Start private chat<ArrowRight aria-hidden="true" /></Button>}
          <Button variant={healthy ? "outline" : "default"} disabled={mutation.busy || !data.available || !data.oauth_configured} onClick={connect}>{data.connected ? "Reconnect Slack" : "Connect Slack"}</Button>
          {!healthy && canStart && <Button variant="outline" disabled={mutation.busy} onClick={onStartPrivateChat}>Start private chat</Button>}
        </div>
        {data.connected && <div className="ns-connection-footer">
          <div className="ns-row-actions"><Button variant="ghost" disabled={mutation.busy || resource.loading} onClick={() => {
            setChecked(false)
            void mutation.run(() => checkPersonalSlack(), () => { setChecked(true); resource.refresh() })
          }}><RefreshCw aria-hidden="true" />Check connection</Button>{checked && !resource.loading && <span className="ns-muted" role="status">Checked</span>}</div>
          <Button variant="ghost" disabled={mutation.busy} onClick={() => setDisconnect(true)}>Disconnect…</Button>
        </div>}
        {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
      </div>
      {disconnect && <Confirm title="Disconnect personal Slack?" description="Future chats can use the workspace Slack connection, if available. Your existing private chats stay private." label="Disconnect personal Slack"
        onConfirm={async () => { await disconnectPersonalSlack(); setChecked(false); resource.refresh() }} onClose={() => setDisconnect(false)} />}
    </section>
  }}</LoadState>
}

function PersonalGranola({ onStartPrivateChat }: { onStartPrivateChat: () => void }) {
  const resource = useResource("/connections/granola/personal", signal => getPersonalGranola({ signal }).then(({ data }) => data))
  const mutation = useMutation(), [disconnect, setDisconnect] = useState(false)
  const cancelled = new URLSearchParams(location.search).get("granola") === "cancelled"
  return <LoadState resource={resource}>{data => <section className="ns-personal-slack" aria-label="Your Granola connection">
    <div className="ns-connection-card">
      <div className="ns-connection-heading">
        <span className="ns-provider-icon"><FileText size={24} aria-hidden="true" /></span>
        <div className="ns-connection-identity"><h2>{data.connected ? "Your Granola notes" : "Connect Granola"}</h2>
          <span className={`ns-connection-status ${data.connected ? "is-connected" : ""}`}><span aria-hidden="true" />{data.connected ? "Connected" : "Not connected"}</span>
        </div>
      </div>
      <p className="ns-help">{data.limitation} Not used in shared chats, Slack threads, or automations.</p>
      <p className="ns-help">Search and read meeting notes, summaries, and available transcripts. Granola plan and workspace permissions apply. Moyai cannot edit notes or switch your Granola workspace.</p>
      {data.connected && <p className="ns-help">Select Granola in your private chat's connections. If access expires or is revoked in Granola, reconnect here.</p>}
      {cancelled && !data.connected && <p className="ns-notice" role="status">Granola sign-in was cancelled. No notes were accessed.</p>}
      {!data.available && <p className="ns-notice" role="status">Sign in with your individual work account to connect Granola.</p>}
      {!data.oauth_configured && <p className="ns-notice" role="status">Ask a workspace administrator to configure Moyai's public HTTPS URL.</p>}
      <div className="ns-row-actions">
        {data.connected && <Button disabled={mutation.busy} onClick={onStartPrivateChat}>Start private chat<ArrowRight aria-hidden="true" /></Button>}
        <Button variant={data.connected ? "outline" : "default"} disabled={mutation.busy || !data.available || !data.oauth_configured} onClick={() => {
          let url: string
          void mutation.run(async () => { url = (await startPersonalGranolaOAuth()).data.url }, () => window.location.assign(productRedirect(url, location.origin)))
        }}>{data.connected ? "Reconnect Granola" : "Connect Granola"}</Button>
      </div>
      {data.connected && <div className="ns-connection-footer"><Button variant="ghost" disabled={mutation.busy} onClick={() => setDisconnect(true)}>Disconnect…</Button></div>}
      {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
    </div>
    {disconnect && <Confirm title="Disconnect Granola?" description="Moyai will stop using this connection, including in active chats. Notes already included in a chat remain in that chat. This does not sign you out of Granola." label="Disconnect Granola"
      onConfirm={async () => { await disconnectPersonalGranola(); resource.refresh() }} onClose={() => setDisconnect(false)} />}
  </section>}</LoadState>
}

function GeneralPage({ onPreferences }: { onPreferences: (preferences: GeneralPreferences) => void }) {
  const resource = useResource("/settings/general", signal => getSettingsGeneral({ signal }).then(({ data }) => data))
  return <LoadState resource={resource}>{data => <GeneralForm initial={data} onPreferences={onPreferences} />}</LoadState>
}

function GeneralForm({ initial, onPreferences }: { initial: GeneralPreferences; onPreferences: (preferences: GeneralPreferences) => void }) {
  const [preferences, setPreferences] = useState(initial)
  const mutation = useMutation()
  const custom = preferences.connection_ids !== null
  const accounts = useResource(custom ? "/connections" : null, loadConnections)
  const save = (patch: Partial<GeneralPreferences>) => {
    let updated: GeneralPreferences
    void mutation.run(async () => { updated = (await updateSettingsGeneral({ body: patch })).data }, () => {
      setPreferences(updated); onPreferences(updated)
    })
  }
  return <>
    <div className="ns-records">
      <div className="ns-record"><div className="ns-record-copy"><h2>Message delivery</h2><p>When you send a message while Moyai is working.</p></div>
        <div className="ns-row-actions"><Choice label="Message delivery" value={preferences.delivery_mode} disabled={mutation.busy} onChange={value => save({ delivery_mode: value as GeneralPreferences["delivery_mode"] })}
          options={[{ value: "when_idle", label: "Queue next" }, { value: "asap", label: "Send now" }]} /></div>
      </div>
      <div className="ns-record"><div className="ns-record-copy"><h2>Connections</h2><p>Use the defaults for each chat, or choose connections for your messages.</p></div>
        <div className="ns-row-actions"><Choice label="Connection preference" value={custom ? "custom" : "default"} disabled={mutation.busy} onChange={value => save({ connection_ids: value === "custom" ? [] : null })}
          options={[{ value: "default", label: "Use chat defaults" }, { value: "custom", label: "Choose connections" }]} /></div>
      </div>
      {custom && <LoadState resource={accounts}>{rows => {
        const available = rows.filter(row => row.enabled)
        return available.length ? available.map(row => <label className="ns-record" key={row.id}>
          <div className="ns-record-copy"><span className="ns-record-title">{row.name}<span className="ns-badge">{row.shared ? "Shared" : "Personal"}</span></span><p>{row.read_only ? "Read only" : "Can make changes"}</p></div>
          <Checkbox aria-label={row.name} checked={preferences.connection_ids?.includes(row.id) ?? false} disabled={mutation.busy || (!preferences.connection_ids?.includes(row.id) && (preferences.connection_ids?.length ?? 0) >= 30)}
            onCheckedChange={checked => save({ connection_ids: checked ? [...(preferences.connection_ids ?? []), row.id] : preferences.connection_ids?.filter(id => id !== row.id) ?? [] })} />
        </label>) : <p className="ns-muted">No connections available.</p>
      }}</LoadState>}
    </div>
    <p className="ns-help ns-footer-help">Only connections available to the chat are used. Personal connections are limited to private chats. <a className="ns-link" href="#settings/connections">Manage connections</a></p>
    {mutation.busy && <p className="ns-muted" role="status">Saving…</p>}
    {mutation.error && <p className="ns-error" role="alert">{mutation.error}</p>}
  </>
}

function ConnectionsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/connections", loadConnections)
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [editor, setEditor] = useState<Connection | "new" | null>(null), [disable, setDisable] = useState<Connection | null>(null)
  const services = [{ value: "github", label: "GitHub" }, { value: "slack", label: "Slack" }, { value: "linear", label: "Linear" }, { value: "notion", label: "Notion" }]
  const connectionBody = (row: Connection) => connectionInput({ name: row.name, kind: row.kind, credential: "", shared: row.shared, read_only: row.read_only, enabled: row.enabled }, row.policy)
  return <LoadState resource={resource}>{data => {
    const rows = data.filter(row => contains(search, row.name, row.kind) && (filter === "all" || row.kind === filter))
    return <><p className="ns-muted">Choose default connections in <a className="ns-link" href="#settings/general">General preferences</a>.</p>
      <Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource} filter={<Choice label="Service" value={filter} onChange={setFilter} options={[{ value: "all", label: "All services" }, ...services]} />}
        action={<Button onClick={() => setEditor("new")}><Plus />Add connection</Button>} />
      {rows.length ? <div className="ns-records">{rows.map(row => <article className="ns-record" key={row.id}><Link2 className="ns-record-icon" aria-hidden="true" />
        <div className="ns-record-copy"><div className="ns-record-title"><h2>{row.name}</h2><span className="ns-badge">{row.shared ? "Shared" : "Personal"}</span></div>
          <p>{services.find(service => service.value === row.kind)?.label ?? row.kind} · {row.enabled ? row.read_only ? "Read only" : row.policy?.require_approval === false ? "Can make changes" : "Changes require approval" : "Disabled"}</p>
          <small>{row.owner_id === user.id ? row.shared && user.role !== "admin" ? "Administrator access is required to edit this shared connection." : "Your connection" : "Managed by another workspace member"}</small></div>
        {row.kind === "granola" ? <a className="ns-link" href="#settings/granola">Manage Granola</a> : row.owner_id === user.id && (!row.shared || user.role === "admin") && <div className="ns-row-actions"><Button variant="outline" size="sm" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.name}</span></Button>{row.enabled && <Button variant="ghost" size="sm" onClick={() => setDisable(row)}>Disable<span className="visually-hidden"> {row.name}</span></Button>}</div>}
      </article>)}</div> : <Empty filtered={Boolean(search) || filter !== "all"} noun="connections" onClear={() => { setSearch(""); setFilter("all") }} />}
      {editor && <Editor title={editor === "new" ? "Add connection" : "Edit connection"} description="Use an access token for the selected service. Interactive OAuth is not supported by this connection form." initial={editor === "new" ? { name: "", kind: "github", credential: "", shared: false, read_only: true, enabled: true } : { name: editor.name, kind: editor.kind, credential: "", shared: editor.shared, read_only: editor.read_only, enabled: editor.enabled }} fields={values => [
        { name: "name", label: "Account name", required: true, maxLength: 150 }, { name: "kind", label: "Service", type: "select", options: services, disabled: editor !== "new" },
        { name: "credential", label: editor === "new" ? "Access token" : "Replacement token", type: "password", required: editor === "new", hint: values.kind === "slack" ? "Use a Slack user token, not a bot token." : values.kind === "notion" ? "Use a Notion OAuth access token, not an integration token." : editor === "new" ? "Token permissions determine which resources this account can reach." : "Leave blank to keep the current token." },
        { name: "read_only", label: "Read only", type: "checkbox" }, { name: "enabled", label: "Enable this connection", type: "checkbox" },
        ...(user.role === "admin" ? [{ name: "shared", label: "Share with workspace members", type: "checkbox" as const }] : []),
      ]} onSave={async values => { await (editor === "new" ? createConnection({ body: connectionInput(values, { require_approval: true }) }) : updateConnection({ path: { connection_id: editor.id }, body: connectionInput(values, editor.policy) })); resource.refresh() }} onClose={() => setEditor(null)} />}
      {disable && <Confirm title={`Disable ${disable.name}?`} description="New work cannot use this connection until you enable it again. The saved credential is kept." label="Disable connection" onConfirm={async () => { await updateConnection({ path: { connection_id: disable.id }, body: { ...connectionBody(disable), enabled: false } }); resource.refresh() }} onClose={() => setDisable(null)} />}
    </>
  }}</LoadState>
}

function UsersPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/users", loadUsers)
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [editor, setEditor] = useState<Account | "new" | null>(null), [remove, setRemove] = useState<Account | null>(null)
  return <LoadState resource={resource}>{data => {
    const product = data.product
    const rows = data.users.filter(row => contains(search, row.username, row.display_name, row.email) && (filter === "all" || (filter === "disabled" ? !row.enabled : row.role === filter)))
    return <><CapabilityNote {...data} />{product && <p className="ns-help">Members join through your workspace sign-in.</p>}<Toolbar search={search} setSearch={setSearch} count={rows.length} resource={resource}
      filter={<Choice label="User role or access" value={filter} onChange={setFilter} options={[{ value: "all", label: "All members" }, { value: "admin", label: "Administrators" }, { value: "member", label: "Members" }, ...product ? [] : [{ value: "disabled", label: "Disabled" }]]} />}
      action={data.capabilities?.create && <Button onClick={() => setEditor("new")}><Plus />Add user</Button>} />
      {rows.length ? <RecordTable label="Workspace users" headings={["User", "Role", "Access", "Actions"]}>{rows.map(row => <TableRow key={row.id}>
        <TableCell><strong>{row.display_name || row.username}{row.id === user.id ? " (you)" : ""}</strong><small>{row.email || row.username}</small></TableCell><TableCell>{row.role === "admin" ? "Administrator" : "Member"}</TableCell><TableCell>{product ? "Organization sign-in" : <span className={`ns-badge ${row.enabled ? "ns-success" : ""}`}>{row.enabled ? "Enabled" : "Disabled"}</span>}</TableCell>
        <TableCell><div className="ns-row-actions">{product && data.capabilities?.roles && row.email && Number.isSafeInteger(row.revision) ? <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit role<span className="visually-hidden"> for {row.email}</span></Button> : !product && data.capabilities?.update && <Button size="sm" variant="outline" onClick={() => setEditor(row)}>Edit<span className="visually-hidden"> {row.username}</span></Button>}
          {data.capabilities?.delete && row.enabled && row.id !== user.id && <Button size="sm" variant="ghost" onClick={() => setRemove(row)}>Disable<span className="visually-hidden"> {row.username}</span></Button>}</div></TableCell>
      </TableRow>)}</RecordTable> : <Empty filtered={Boolean(search) || filter !== "all"} noun="users" onClear={() => { setSearch(""); setFilter("all") }} />}
      {product && data.capabilities?.roles && editor && editor !== "new" && <Editor title="Edit workspace role" description={`Change the role for ${editor.email}. Organization sign-in continues to manage this account. At least one administrator must remain.`}
        initial={{ role: editor.role }} fields={[{ name: "role", label: "Role", type: "select", options: [{ value: "member", label: "Member" }, { value: "admin", label: "Administrator" }] }]}
        onSave={async values => {
          if (values.role === editor.role) return
          await updateProductUserRole({ body: { email: editor.email ?? "", role: values.role === "admin" ? "admin" : "member", revision: editor.revision ?? 0 } })
          resource.refresh(); window.dispatchEvent(new Event("moyai:access-changed"))
        }} onClose={() => setEditor(null)} />}
      {!product && editor && <Editor title={editor === "new" ? "Add user" : "Edit user"} description={editor === "new" ? "Share new credentials through an approved channel." : "Saving changes revokes existing sign-ins and stops this user’s pending or running work. Saved history is retained."} initial={editor === "new" ? { username: "", display_name: "", password: "", role: "member", enabled: true } : { username: editor.username, display_name: editor.display_name, password: "", role: editor.role, enabled: editor.enabled }} fields={[
        { name: "username", label: "Username", required: true, disabled: editor !== "new", maxLength: 200, pattern: "[A-Za-z0-9][A-Za-z0-9_.@+\\-]*" }, { name: "display_name", label: "Display name", required: true, maxLength: 200 },
        { name: "password", label: editor === "new" ? "Password" : "New password", type: "password", required: editor === "new", minLength: 12, maxLength: 4096, hint: editor === "new" ? "Use at least 12 characters and a unique password." : "Leave blank to keep the existing password; replacements need at least 12 characters." },
        { name: "role", label: "Role", type: "select", options: [{ value: "member", label: "Member" }, { value: "admin", label: "Administrator" }], disabled: editor !== "new" && editor.id === user.id },
        { name: "enabled", label: "Enable workspace access", type: "checkbox", disabled: editor !== "new" && editor.id === user.id },
      ]} onSave={async values => {
        const role = values.role === "admin" ? "admin" as const : "member" as const
        if (editor === "new") await createSettingsUser({ body: { username: String(values.username), display_name: String(values.display_name), password: String(values.password), role, enabled: values.enabled === true } })
        else {
          const body: UserEditWritable = {}
          if (values.display_name !== editor.display_name) body.display_name = String(values.display_name)
          if (values.role !== editor.role) body.role = role
          if (values.enabled !== editor.enabled) body.enabled = values.enabled === true
          if (values.password) body.password = String(values.password)
          if (!Object.keys(body).length) return
          await updateSettingsUser({ path: { identity: editor.id }, body })
        }
        resource.refresh()
      }} onClose={() => setEditor(null)} />}
      {remove && <Confirm title={`Disable ${remove.display_name || remove.username}?`} description="This revokes workspace access and existing sign-ins, and stops this user’s pending or running work. Saved history is retained." label="Disable user" onConfirm={async () => { await deleteSettingsUser({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function AutomationEditor({ user, onClose, refresh }: { user: SettingsUser; onClose: () => void; refresh: () => void }) {
  const sessions = useResource("/sessions", loadSessions)
  const models = useResource("/me", signal => getMe({ signal }).then(({ data }) => data))
  const connections = useResource("/connections", loadConnections)
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
      await createSettingsAutomation({ body: { name: String(values.name), prompt: String(values.prompt), session_id: String(values.session_id), model: String(values.model), interval_minutes: Number(values.interval_minutes), connections: selectedConnections } }); refresh()
    }} onClose={onClose} />
}

function AutomationLogs({ automation, onClose }: { automation: SettingsAutomation; onClose: () => void }) {
  const resource = useResource(`/settings/automations/${encodeURIComponent(automation.id)}/logs`, signal => listSettingsAutomationLogs({ path: { identity: automation.id }, signal }).then(({ data }) => data))
  const returnFocus = useRef<HTMLElement | null>(document.activeElement instanceof HTMLElement ? document.activeElement : null)
  return <Dialog open onOpenChange={open => { if (!open) onClose() }}><DialogContent className="native-settings-dialog" onCloseAutoFocus={event => { event.preventDefault(); returnFocus.current?.focus() }}>
    <DialogHeader><DialogTitle>{automation.name}: run history</DialogTitle><DialogDescription>Recorded admissions and their status. Open the session to review a turn’s output.</DialogDescription></DialogHeader>
    <LoadState resource={resource}>{data => data.logs.length ? <RecordTable label="Automation run history" headings={["Created", "Status", "Turn"]}>{data.logs.map(row => <TableRow key={row.id}><TableCell>{dateTime(row.created_at)}</TableCell><TableCell>{row.status}</TableCell><TableCell>{row.turn_id ? <a className="ns-link" href={`#${automation.session_id}`}>{row.turn_id}</a> : "No turn admitted"}</TableCell></TableRow>)}</RecordTable> : <p className="ns-muted">No recorded runs yet.</p>}</LoadState>
    <Button variant="outline" disabled={resource.loading} onClick={resource.refresh}><RefreshCw />Refresh history</Button>
  </DialogContent></Dialog>
}

function AutomationsPage({ user }: { user: SettingsUser }) {
  const resource = useResource("/settings/automations", signal => listSettingsAutomations({ signal }).then(({ data }) => data))
  const [search, setSearch] = useState(""), [filter, setFilter] = useState("all")
  const [create, setCreate] = useState(false), [remove, setRemove] = useState<SettingsAutomation | null>(null), [logs, setLogs] = useState<SettingsAutomation | null>(null)
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
          {data.capabilities?.update && <Button variant="outline" size="sm" disabled={mutation.busy} onClick={() => void mutation.run(() => updateSettingsAutomation({ path: { identity: row.id }, body: { paused: !row.paused } }), resource.refresh)}>{row.paused ? "Resume" : "Pause"}<span className="visually-hidden"> {row.name}</span></Button>}
          {data.capabilities?.delete && <Button variant="ghost" size="icon-sm" aria-label={`Delete ${row.name}`} disabled={mutation.busy} onClick={() => setRemove(row)}><Trash2 /></Button>}</div>
      </article>)}</div> : <Empty filtered={Boolean(search) || filter !== "all"} noun="automations" onClear={() => { setSearch(""); setFilter("all") }} />}
      {create && <AutomationEditor user={user} refresh={resource.refresh} onClose={() => setCreate(false)} />}
      {logs && <AutomationLogs automation={logs} onClose={() => setLogs(null)} />}
      {remove && <Confirm title={`Delete ${remove.name}?`} description="Future scheduled admissions stop. Work already admitted to a session and its history are kept." label="Delete automation" onConfirm={async () => { await deleteSettingsAutomation({ path: { identity: remove.id } }); resource.refresh() }} onClose={() => setRemove(null)} />}
    </>
  }}</LoadState>
}

function OverviewPage({ user, navigate, productMode }: { user: SettingsUser; navigate: (page: string) => void; productMode: "standalone" | "existing" }) {
  // Native-store totals do not describe canonical product data. Navigation
  // remains available without publishing false zero counts during cutover.
  const resource = useResource(productMode === "existing" ? null : "/settings/overview", signal => getSettingsOverview({ signal }).then(({ data }) => data))
  return <>
    {resource.error && <p className="ns-notice" role="alert">Overview counts could not be loaded. {resource.error} <Button variant="ghost" onClick={resource.refresh}>Try again</Button></p>}
    {resource.loading && <p className="ns-muted" role="status">Loading overview counts…</p>}
    <div className="ns-overview-grid">{groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section key={group} className="ns-overview-group"><div className="ns-section-heading"><h2>{group}</h2>{group === "Administration" && <span className="ns-badge">Admin</span>}</div>
      <div>{destinations.filter(item => item.group === group && (!item.productOnly || productMode === "existing")).map(item => <SettingsLink key={item.id} page={item.id} navigate={navigate} className="ns-overview-link"><item.Icon size={20} aria-hidden="true" /><span><strong>{item.title}</strong><span>{item.description}</span></span>
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

export function SettingsPage({ user, page, productMode = "standalone", onNavigate, onBack, onStartPrivateChat, sidebarCollapsed, onToggleSidebar, onPreferences }: SettingsPageProps) {
  const current = !page || page === "settings" ? "overview" : page === "spend" ? "usage" : page
  const destination = destinations.find(item => item.id === current)
  const [navOpen, setNavOpen] = useState(false)
  const heading = useRef<HTMLHeadingElement>(null)
  const content = useRef<HTMLDivElement>(null)
  const title = current === "overview" ? "Settings" : destination?.title ?? "Page not found"
  const reportPage = user.role === "admin" && (current === "performance" || current === "usage")
  useEffect(() => {
    setNavOpen(false); heading.current?.focus(); content.current?.scrollTo({ top: 0 })
    const previous = document.title
    document.title = `${title} · Moyai`
    return () => { document.title = previous }
  }, [current, title])
  const navigate = (value: string) => { setNavOpen(false); onNavigate(value) }
  const navLink = (value: string, label: string, icon: React.ReactNode) => <SettingsLink page={value} navigate={navigate} aria-current={current === value ? "page" : undefined}>{icon}<span>{label}</span></SettingsLink>
  return <div className={`native-settings ${sidebarCollapsed ? "ns-sidebar-collapsed" : ""}`}>
    <a className="ns-skip" href="#native-settings-content" onClick={event => { event.preventDefault(); heading.current?.focus() }}>Skip to settings content</a>
    <aside className="ns-rail">
      <div className="ns-brand-row"><MoyaiBrand onClick={event => { if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) { event.preventDefault(); onBack() } }} /><SidebarToggle collapsed={sidebarCollapsed} onToggle={onToggleSidebar} /><Button className="ns-nav-toggle" size="icon" variant="ghost" aria-label="Toggle settings navigation" aria-expanded={navOpen} aria-controls="native-settings-navigation" onClick={() => setNavOpen(value => !value)}><Menu /></Button></div>
      <Button className="ns-back" variant="ghost" onClick={onBack}><ArrowLeft size={16} />Back to workspace</Button>
      <nav id="native-settings-navigation" className={navOpen ? "ns-navigation open" : "ns-navigation"} aria-label="Settings">
        <p className="ns-nav-title">Settings</p>{navLink("overview", "Overview", <LayoutGrid size={17} aria-hidden="true" />)}
        {groups.filter(group => group !== "Administration" || user.role === "admin").map(group => <section className="ns-nav-group" key={group}><h2>{group}</h2>{destinations.filter(item => item.group === group && (!item.productOnly || productMode === "existing")).map(item => <React.Fragment key={item.id}>{navLink(item.id, item.title, <item.Icon size={17} aria-hidden="true" />)}</React.Fragment>)}</section>)}
      </nav><div className="ns-account"><span>{user.name}</span><small>{user.role === "admin" ? "Administrator" : "Workspace member"}</small></div>
    </aside>
    <div className="ns-main"><div className="ns-breadcrumb" aria-label="Breadcrumb"><SettingsLink page="overview" navigate={navigate}>Settings</SettingsLink>{current !== "overview" && <><ChevronRight size={14} aria-hidden="true" /><span>{title}</span></>}<AppearanceToggle /></div>
      <div className="ns-content" id="native-settings-content" ref={content}>{!reportPage && <header className="ns-page-heading"><h1 ref={heading} tabIndex={-1}>{title}</h1>{destination?.description && current !== "general" && <p>{destination.description}</p>}</header>}
        <div key={`${user.id}:${current}`}>
          {destination?.productOnly && productMode !== "existing" ? <div className="ns-empty"><h2>Existing product accounts required</h2><p>This page is available when Moyai uses the existing product account service.</p></div>
            : destination?.admin && user.role !== "admin" ? <div className="ns-empty" role="alert"><Shield size={24} /><h2>Administrator access required</h2><p>This page is available to workspace administrators.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview</Button></div>
            : current === "overview" ? <OverviewPage user={user} navigate={navigate} productMode={productMode} />
              : current === "general" ? <GeneralPage onPreferences={onPreferences} />
              : current === "skills" ? productMode === "existing" ? <ProductSkillsPage /> : <SkillsPage user={user} />
                : current === "automations" ? productMode === "existing" ? <ProductAutomationsPage userId={user.id} /> : <AutomationsPage user={user} />
                  : current === "memory" ? productMode === "existing" ? <ProductMemoryPage /> : <MemoryPage />
                    : current === "connections" ? productMode === "existing" ? <ProductConnectionsPage user={user} /> : <ConnectionsPage user={user} />
                      : current === "granola" ? <PersonalGranola onStartPrivateChat={onStartPrivateChat} />
                      : current === "personal-slack" ? <ProductPersonalSlack onStartPrivateChat={onStartPrivateChat} individual={user.individual !== false} />
                      : current === "secrets" ? productMode === "existing" ? <ProductSecretsPage user={user} /> : <SecretsPage user={user} />
                        : current === "users" ? <UsersPage user={user} />
                          : current === "performance" || current === "usage" ? <AnalyticsPage kind={current} productMode={productMode} headingRef={heading} />
                            : <div className="ns-empty"><p>This settings page does not exist.</p><Button variant="outline" onClick={() => navigate("overview")}>Back to overview<ArrowRight /></Button></div>}
        </div>
      </div>
    </div>
  </div>
}
