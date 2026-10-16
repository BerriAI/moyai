import React, { useEffect, useId, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { api, csrfToken } from "./api"
import "./settings.css"

type CredentialRequest = {
  request_id: string; generation: number; status: string; provider: string; name: string; format: string; env_var: string
  reason: string; setup_url: string; setup_instructions: string; can_resolve: boolean
  input_fields: { name: string; label: string; secret: boolean; required: boolean }[]
  credentials: { id: string; label: string; scope: string; lifetime: string; revision: number }[]
  scopes: string[]; lifetimes: string[]
  note?: string
}
const errorText = (value: unknown) => value instanceof Error ? value.message : "The request could not be completed."

/** The caller passes only the server-authored request_id from deferred-call metadata. */
export function ProductCredentialRequest({ requestId, onResolved }: { requestId: string; onResolved: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false), [version, setVersion] = useState(0)
  const [resource, setResource] = useState<CredentialRequest | null>(null)
  const [loading, setLoading] = useState(false), [busy, setBusy] = useState(false), [error, setError] = useState("")
  const [selected, setSelected] = useState("__new__"), [scope, setScope] = useState(""), [lifetime, setLifetime] = useState("")
  const [label, setLabel] = useState(""), [expires, setExpires] = useState(""), [value, setValue] = useState("")
  const [fields, setFields] = useState<Record<string, string>>({})
  const pending = useRef(false), mounted = useRef(true), file = useRef<HTMLInputElement>(null)
  const id = useId()
  const clearValues = () => { setValue(""); setFields({}); if (file.current) file.current.value = "" }
  useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [])
  useEffect(() => {
    const revoked = () => { clearValues(); setResource(null); setOpen(false) }
    window.addEventListener("moyai:access-changed", revoked)
    window.addEventListener("moyai:signed-out", revoked)
    return () => { window.removeEventListener("moyai:access-changed", revoked); window.removeEventListener("moyai:signed-out", revoked) }
  }, [])
  useEffect(() => {
    if (!open) return
    const controller = new AbortController(), auth = csrfToken()
    setLoading(true); setError(""); setResource(null); clearValues()
    void api<CredentialRequest>(`/credentials/requests/${encodeURIComponent(requestId)}`, "GET", undefined, controller.signal).then(result => {
      if (!controller.signal.aborted && csrfToken() === auth) {
        setResource(result); setSelected(result.scopes.length ? "__new__" : result.credentials[0]?.id || "__new__")
        setScope(""); setLifetime(""); setLabel(result.name); setExpires("")
      }
    }, reason => { if (!controller.signal.aborted && csrfToken() === auth) setError(errorText(reason)) }).finally(() => {
      if (!controller.signal.aborted && csrfToken() === auth) setLoading(false)
    })
    return () => controller.abort()
  }, [open, requestId, version])

  const resolve = async (decline: boolean) => {
    if (!resource || pending.current) return
    const auth = csrfToken()
    setError("")
    let body: Record<string, unknown> = { generation: resource.generation, decision: decline ? "decline" : "provide" }
    if (!decline) {
      if (selected !== "__new__") body.secret_id = selected
      else {
        if (!scope || !lifetime) { setError("Choose who can use this access and how long to save it."); return }
        if (resource.provider === "generic" && resource.format === "env" && !resource.input_fields.length) {
          setError("This request must declare its credential input fields before new access can be supplied."); return
        }
        const supplied = resource.provider === "generic" && resource.format === "env" ? JSON.stringify(
          Object.fromEntries(resource.input_fields.filter(field => fields[field.name]).map(field => [field.name, fields[field.name]])),
        ) : value
        const expiration = expires ? new Date(expires) : null
        if (expiration && !Number.isFinite(expiration.valueOf())) { setError("Choose a valid expiry date and time."); return }
        body = { ...body, scope, lifetime, label, value: supplied, expires_at: expiration?.toISOString() ?? "" }
      }
    }
    pending.current = true; setBusy(true)
    try {
      await api(`/credentials/requests/${encodeURIComponent(requestId)}`, "POST", body)
      if (mounted.current && csrfToken() === auth) { clearValues(); setOpen(false); await onResolved() }
    } catch (reason) {
      if (mounted.current && csrfToken() === auth) setError(errorText(reason))
    } finally { pending.current = false; if (mounted.current && csrfToken() === auth) setBusy(false) }
  }

  const choice = (name: string, current: string, change: (value: string) => void, options: { value: string; label: string }[]) =>
    <Select value={current} onValueChange={change} disabled={busy}><SelectTrigger aria-label={name}><SelectValue placeholder={`Choose ${name.toLowerCase()}…`} /></SelectTrigger><SelectContent className="native-settings-select">{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select>

  return <Dialog open={open} onOpenChange={next => { if (!pending.current) { if (!next) clearValues(); setOpen(next) } }}>
    <DialogTrigger asChild><Button type="button" variant="outline">Review secure access request</Button></DialogTrigger>
    <DialogContent className="native-settings-dialog" onEscapeKeyDown={event => { if (pending.current) event.preventDefault() }}>
      <DialogHeader><DialogTitle>Provide secure access</DialogTitle><DialogDescription>Values go to the encrypted credential store. They are not sent in chat or supplied as model tool arguments.</DialogDescription></DialogHeader>
      {loading && <p role="status">Loading the current request…</p>}
      {error && <p className="ns-error" role="alert">{error}<Button type="button" variant="outline" disabled={busy} onClick={() => setVersion(previous => previous + 1)}>Reload request</Button></p>}
      {resource && <form className="ns-fields" onSubmit={event => { event.preventDefault(); event.stopPropagation(); void resolve(false) }}>
        <p>{resource.reason}</p>
        {resource.note && <p className="ns-muted">{resource.note}</p>}
        {resource.setup_instructions && <p className="ns-muted">{resource.setup_instructions}</p>}
        {!resource.can_resolve ? <p role="status">Request status: {resource.status}. Complete any other current questions or approvals to continue.</p> : <>
          {resource.credentials.length > 0 && <div className="ns-field"><label>Credential source</label>{choice("Credential source", selected, next => { clearValues(); setSelected(next) }, [
            ...(resource.scopes.length ? [{ value: "__new__", label: "Provide new access" }] : []),
            ...resource.credentials.map(item => ({ value: item.id, label: `${item.label} · ${item.scope} · ${item.lifetime}` })),
          ])}</div>}
          {selected === "__new__" && resource.scopes.length > 0 && <>
            <div className="ns-field"><label htmlFor={`${id}-label`}>Name</label><Input id={`${id}-label`} required maxLength={80} value={label} disabled={busy} onChange={event => setLabel(event.target.value)} /></div>
            {resource.provider === "generic" && resource.format === "env" ? resource.input_fields.map(field => <div className="ns-field" key={field.name}>
              <label htmlFor={`${id}-${field.name}`}>{field.label}</label><Input id={`${id}-${field.name}`} type={field.secret ? "password" : "text"} autoComplete="off" required={field.required} maxLength={32768} disabled={busy} value={fields[field.name] ?? ""} onChange={event => setFields(previous => ({ ...previous, [field.name]: event.target.value }))} />
            </div>) : resource.format === "file" ? <div className="ns-field"><label htmlFor={`${id}-file`}>Credential file</label><Input ref={file} id={`${id}-file`} type="file" required disabled={busy} onChange={async event => {
              const selectedFile = event.target.files?.[0]
              setValue("")
              if (!selectedFile) return
              if (selectedFile.size > 128 * 1024) { setError("Credential files must be UTF-8 text up to 128 KiB."); return }
              const auth = csrfToken()
              try {
                const text = await selectedFile.text()
                if (mounted.current && csrfToken() === auth && file.current?.files?.[0] === selectedFile) setValue(text)
              } catch { if (mounted.current && csrfToken() === auth) setError("The credential file could not be read.") }
            }} /></div> : <div className="ns-field"><label htmlFor={`${id}-value`}>Access key</label><Input id={`${id}-value`} type="password" autoComplete="new-password" required disabled={busy} value={value} onChange={event => setValue(event.target.value)} /></div>}
            <div className="ns-field"><label>Who can use this access</label>{choice("Sharing", scope, setScope, resource.scopes.map(item => ({ value: item, label: item === "personal" ? "Only me" : "Organization" })))}</div>
            <div className="ns-field"><label>Save for</label>{choice("Lifetime", lifetime, setLifetime, resource.lifetimes.map(item => ({ value: item, label: item === "session" ? "This session" : "Future sessions" })))}</div>
            <div className="ns-field"><label htmlFor={`${id}-expires`}>Expiry (optional, local time)</label><Input id={`${id}-expires`} type="datetime-local" value={expires} disabled={busy} onChange={event => setExpires(event.target.value)} /></div>
            {resource.name === "webhook-signing-secret" && <p className="ns-field-hint">Webhook receiver setup needs persistent reuse without expiry. Configuring the receiver copies this secret into its saved configuration.</p>}
          </>}
          <div className="ns-dialog-actions"><Button type="button" variant="outline" disabled={busy} onClick={() => void resolve(true)}>Decline</Button><Button type="submit" disabled={busy || (selected === "__new__" && (!resource.scopes.length || (resource.format === "file" && !value)))}>{busy ? "Saving…" : "Provide access"}</Button></div>
        </>}
        <Button type="button" variant="ghost" disabled={busy} onClick={() => { clearValues(); setOpen(false) }}>Close</Button>
      </form>}
    </DialogContent>
  </Dialog>
}
