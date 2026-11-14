import React, { useCallback, useEffect, useId, useRef, useState } from "react"
import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import { Input } from "@/components/ui/input"
import { ApiError, csrfToken } from "./api"
import {
  connectGitLabOrganization, discoverGitLabProjects, listGitLabProjects, selectGitLabProjects,
  type GitLabProjectDiscoveryResult, type OrganizationConnection,
} from "./generated/api"

/** The token remains in this mounted form until save, cancellation, or access loss. */
export function ProductGitLabSetup({ connection, mode, onDone, onBack, onBusyChange }: {
  connection: OrganizationConnection; mode: "connect" | "projects"
  onDone: () => void; onBack: () => void; onBusyChange: (busy: boolean) => void
}) {
  const id = useId()
  const [token, setToken] = useState("")
  const [baseUrl, setBaseUrl] = useState(connection.base_url || "https://gitlab.com")
  const [result, setResult] = useState<GitLabProjectDiscoveryResult | null>(null)
  const [repositories, setRepositories] = useState<GitLabProjectDiscoveryResult["repositories"]>([])
  const [known, setKnown] = useState<GitLabProjectDiscoveryResult["repositories"]>(connection.repositories || [])
  const [selected, setSelected] = useState<number[]>([])
  const [search, setSearch] = useState(""), [activeSearch, setActiveSearch] = useState("")
  const [busy, setBusy] = useState(false), [error, setError] = useState("")
  const initialized = useRef(false), alive = useRef(true)
  const pending = useRef<AbortController | null>(null)

  useEffect(() => { onBusyChange(busy); return () => onBusyChange(false) }, [busy, onBusyChange])
  useEffect(() => {
    alive.current = true
    const clear = () => {
      pending.current?.abort(); pending.current = null
      setToken(""); setResult(null); setRepositories([]); setKnown([]); setSelected([]); setBusy(false)
    }
    window.addEventListener("moyai:access-changed", clear)
    window.addEventListener("moyai:signed-out", clear)
    return () => {
      alive.current = false; pending.current?.abort(); pending.current = null
      window.removeEventListener("moyai:access-changed", clear)
      window.removeEventListener("moyai:signed-out", clear)
    }
  }, [])

  const run = useCallback(async <T,>(action: (signal: AbortSignal) => Promise<T>, done: (data: T) => void) => {
    if (pending.current) return
    const requestAuth = csrfToken(), controller = new AbortController()
    pending.current = controller; setBusy(true); setError("")
    const current = () => alive.current && !controller.signal.aborted && csrfToken() === requestAuth
    try {
      const data = await action(controller.signal)
      if (current()) done(data)
    } catch (failure) {
      if (current()) {
        setError(failure instanceof Error ? failure.message : "The GitLab request could not be completed.")
        if (failure instanceof ApiError && [401, 403].includes(failure.status)) {
          window.dispatchEvent(new Event("moyai:access-changed"))
        }
        if (failure instanceof ApiError && failure.status === 409) {
          setResult(null); setRepositories([]); setSelected([]); initialized.current = false
        }
      }
    } finally {
      if (pending.current === controller) pending.current = null
      if (current()) setBusy(false)
    }
  }, [])

  const receive = useCallback((data: GitLabProjectDiscoveryResult, append: boolean) => {
    setResult(data)
    setRepositories(previous => append
      ? [...new Map([...previous, ...data.repositories].map(row => [row.id, row])).values()]
      : data.repositories)
    setKnown(previous => [...new Map([...previous, ...data.repositories].map(row => [row.id, row])).values()])
    if (!initialized.current) {
      initialized.current = true; setSelected(data.selected_ids)
    }
  }, [])

  useEffect(() => {
    if (mode === "projects") void run(
      signal => listGitLabProjects({ signal }).then(({ data }) => data),
      data => receive(data, false),
    )
  }, [mode, run, receive])

  const discover = (page = 1, query = search, append = false) => void run(
    signal => mode === "projects"
      ? listGitLabProjects({ query: { page, search: query, revision: result?.revision }, signal }).then(({ data }) => data)
      : discoverGitLabProjects({ body: { token, base_url: baseUrl, page, search: query, revision: result?.revision }, signal }).then(({ data }) => data),
    data => { receive(data, append); setActiveSearch(query) },
  )
  const resetDiscovery = () => {
    initialized.current = false; setResult(null); setRepositories([]); setKnown([]); setSelected([]); setError("")
  }
  const toggle = (identity: number, checked: boolean) => setSelected(previous =>
    checked ? [...new Set([...previous, identity])] : previous.filter(value => value !== identity))
  const save = () => {
    if (!result || !selected.length) return
    const body = { repository_ids: selected, revision: result.revision }
    void run(
      signal => mode === "projects"
        ? selectGitLabProjects({ body, signal })
        : connectGitLabOrganization({ body: { ...body, token, base_url: result.base_url }, signal }),
      () => { setToken(""); onDone() },
    )
  }

  return <div className="ns-fields">
    {mode === "connect" && <form className="ns-fields" onSubmit={event => { event.preventDefault(); setSearch(""); discover(1, "") }}>
      <p>Use a personal or project access token with <code>api</code> scope, then choose the projects Moyai can use.</p>
      <div className="ns-field"><label htmlFor={`${id}-origin`}>GitLab URL</label>
        <Input id={`${id}-origin`} type="url" required maxLength={2048} disabled={busy} value={baseUrl}
          onChange={event => { setBaseUrl(event.target.value); resetDiscovery() }} />
        <p className="ns-field-hint">GitLab.com or your self-hosted HTTPS origin. Local installations may use a literal loopback HTTP address.</p>
      </div>
      <div className="ns-field"><label htmlFor={`${id}-token`}>GitLab access token</label>
        <Input id={`${id}-token`} type="password" autoComplete="off" required maxLength={4096} disabled={busy} value={token}
          onChange={event => { setToken(event.target.value); resetDiscovery() }} />
        <p className="ns-field-hint">The token is stored encrypted after you save the projects. It is never placed in chat or sent to the sandbox.</p>
      </div>
      <Button type="submit" variant={result ? "outline" : "default"} disabled={busy || !token.trim()}>
        {busy && !result ? "Discovering projects…" : "Discover projects"}
      </Button>
    </form>}
    {mode === "projects" && <p>Choose the projects available to organization sessions. Saving replaces the selection and invalidates earlier project grants.</p>}
    {(result || mode === "projects") && <>
      <form className="ns-fields" onSubmit={event => { event.preventDefault(); discover() }}>
        <div className="ns-field"><label htmlFor={`${id}-search`}>Search GitLab projects</label>
          <Input id={`${id}-search`} type="search" maxLength={200} disabled={busy} value={search} onChange={event => setSearch(event.target.value)} />
        </div>
        <div className="ns-row-actions"><Button type="submit" variant="outline" disabled={busy}>Search projects</Button>
          {search && <Button type="button" variant="ghost" disabled={busy} onClick={() => { setSearch(""); discover(1, "") }}>Clear search</Button>}</div>
      </form>
      {result && <>
        <p className="ns-muted">{result.base_url} · {selected.length} selected</p>
        <div className="ns-fields" role="group" aria-label="GitLab projects">
          {repositories.map(project => <label className="check-label" key={project.id}>
            <Checkbox checked={selected.includes(project.id)} disabled={busy} onCheckedChange={checked => toggle(project.id, checked === true)} />
            <span>{project.full_name} <small>#{project.id}</small></span>
          </label>)}
          {!repositories.length && <p role="status">No projects found. Try another search or check that the token has project membership and api scope.</p>}
        </div>
        {result.next_page !== null && <Button variant="outline" disabled={busy} onClick={() => discover(result.next_page ?? 1, activeSearch, true)}>Load more projects</Button>}
        {result.truncated && <p className="ns-notice" role="status">The project listing reached its limit. Narrow the search to find remaining projects.</p>}
        {selected.length > 0 && <details><summary>Selected projects ({selected.length})</summary><ul className="ns-supporting-files">
          {selected.map(identity => <li key={identity}>{known.find(row => row.id === identity)?.full_name || `Project #${identity}`}{" "}
            <Button variant="ghost" size="sm" disabled={busy} aria-label={`Remove project ${identity}`} onClick={() => toggle(identity, false)}>Remove</Button>
          </li>)}
        </ul></details>}
      </>}
    </>}
    {busy && <p role="status">Checking GitLab access…</p>}
    {error && <p className="ns-error" role="alert">{error}</p>}
    <div className="ns-dialog-actions">
      <Button variant="outline" disabled={busy} onClick={() => { setToken(""); onBack() }}>Back to account</Button>
      {result && <Button disabled={busy || !selected.length || selected.length > 200} onClick={save}>{busy ? "Saving…" : "Save selected projects"}</Button>}
    </div>
    {selected.length > 200 && <p className="ns-error" role="alert">Select at most 200 projects.</p>}
  </div>
}
