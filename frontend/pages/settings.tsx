import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { queryClient, sessionQuery, titleQuery, unwrap } from "../api/client"
import { getSessionTitleConfiguration, updateChatPreferences, updateSessionTitleConfiguration } from "../api/generated/sdk.gen"
import type { WorkspaceSession } from "../api/generated/types.gen"
import type { Preferences } from "../legacy/bridge"
import { Button } from "../components/ui/button"
import { Input } from "../components/ui/input"
import { Switch } from "../components/ui/switch"
import { Label } from "../components/ui/label"
import { groups, visible } from "./settings-navigation"

function Preference({ name, title, description, value }: { name: keyof Preferences; title: string; description: string; value: boolean }) {
  const id = name.replaceAll("_", "-")
  const userId = window.MoyaiControllers.userId()
  const [status, setStatus] = useState("")
  const save = useMutation({
    mutationFn: async (checked: boolean) => unwrap(await updateChatPreferences({ body: { [name]: checked } })),
    onSuccess(saved) {
      if (window.MoyaiControllers.userId() !== userId) return
      const changed = { [name]: saved[name] }
      window.MoyaiControllers.updatePreferences(changed)
      window.MoyaiControllers.invalidate()
      queryClient.setQueryData(sessionQuery().queryKey, (current: WorkspaceSession | undefined) => current && ({ ...current, preferences: { ...current.preferences, ...changed } }))
      setStatus(" · Saved")
    },
    onError(error) { setStatus(` · Could not confirm the change. ${error.message} Reload settings to check, or try again.`) },
  })
  return <div className="settings-preference">
    <Label htmlFor={id}><span className="settings-copy"><strong id={`${id}-title`}>{title}</strong><span id={`${id}-description`}>{description}</span></span>
      <Switch id={id} checked={value} disabled={save.isPending} onCheckedChange={checked => { setStatus(""); save.mutate(checked) }} aria-labelledby={`${id}-title`} aria-describedby={`${id}-description ${id}-status`} />
    </Label>
    <p id={`${id}-status`} className="subtext" role="status">{save.isPending ? "Saving…" : `${value ? "On" : "Off"}${status}`}</p>
  </div>
}

export function SettingsPage() {
  const session = useQuery(sessionQuery())
  const titles = useQuery(titleQuery)
  const [model, setModel] = useState("")
  const [titleStatus, setTitleStatus] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)
  const admin = session.data?.role === "admin"
  useEffect(() => { if (session.data) window.MoyaiControllers.applySession(session.data) }, [session.data])
  useEffect(() => { if (titles.data) setModel(titles.data.model) }, [titles.data])
  useEffect(() => { heading.current?.focus({ preventScroll: true }) }, [session.isSuccess])
  const saveTitle = useMutation({
    mutationFn: async () => {
      unwrap(await updateSessionTitleConfiguration({ body: { model: model.trim() } }))
      return unwrap(await getSessionTitleConfiguration())
    },
    onSuccess(saved) {
      queryClient.setQueryData(titleQuery.queryKey, saved)
      window.MoyaiControllers.invalidate()
      setModel(saved.model)
      setTitleStatus(`Saved. New title attempts use ${saved.model}.`)
    },
    onError(error) { setTitleStatus(error.message) },
  })
  if (session.isPending) return <p className="subtext" role="status">Loading settings…</p>
  if (session.isError) return <section className="settings-load-error"><h1>Settings</h1><div role="alert"><h2>Unable to load this page</h2><p>{session.error.message}</p><p>Check your connection and try again.</p></div><Button id="settings-retry" onClick={() => void session.refetch()}>Try again</Button></section>
  if (!session.data.authenticated) return <p role="alert">Please sign in to view Settings.</p>
  const role = session.data.role ?? "member"
  const titleHelp = titles.data ? !titles.data.enabled ? "Title generation is disabled by the server." : !titles.data.gateway_configured ? "Gateway access must be configured on the server." : admin ? "Enter the exact model ID enabled on your gateway." : "Only administrators can change the workspace title model." : "Loading…"
  return <section className="settings-page">
    <div className="page-heading"><div><h1 ref={heading} tabIndex={-1}>Settings</h1><p className="subtext">Make Moyai work the way your team does.</p></div></div>
    <div className="settings-grid">{groups.filter(group => !group.admin || admin).map(group => <section className="settings-group" aria-labelledby={`settings-${group.id}`} key={group.id}>
      <h2 id={`settings-${group.id}`}>{group.title}{group.admin && <span>Admin</span>}</h2>
      <div className="settings-links">{group.items.filter(item => visible(item, role)).map(item => <a key={item.view} className="settings-link" href={`#${item.view}`}>
        <span className="settings-icon"><item.icon size={24} strokeWidth={1.6} /></span><span className="settings-copy"><strong>{item.title}</strong><span>{item.description}</span></span><span className="settings-arrow" aria-hidden="true">›</span>
      </a>)}</div>
    </section>)}</div>
    <section className="card settings-form-section" id="title-model-settings"><div><h2>Session titles</h2><p className="subtext">Choose the model that names new chats. Existing titles and the chat model stay the same.</p></div>
      <form id="title-model-form" onSubmit={event => { event.preventDefault(); setTitleStatus(""); saveTitle.mutate() }}><Label htmlFor="title-model">Gateway model ID</Label><Input id="title-model" maxLength={200} required placeholder="openai/gpt-4.1-nano" aria-describedby="title-model-status" disabled={!admin || titles.isPending} value={model} onChange={event => setModel(event.target.value)} /><Button type="submit" disabled={!admin || saveTitle.isPending || titles.isPending}>Save model</Button><p id="title-model-status" role="status">{titleStatus || titles.error?.message || titleHelp}</p></form>
    </section>
    <section className="settings-group" aria-labelledby="preferences-title"><div><h2 id="preferences-title">Preferences</h2><p className="subtext">Saved for your account.{!session.data.identity && " Shared password and local sign-ins use a shared profile."}</p></div>
      <div className="settings-preferences">
        <Preference name="send_immediately" title="Send messages immediately" description="Send follow-ups into the active response instead of queueing them while Moyai is working." value={session.data.preferences.send_immediately ?? false} />
        <Preference name="omit_private_tool_payloads" title="Hide private tool content in traces" description="Hide inputs and results from credential, skill, memory, and connector tools in new responses. Secret redaction stays on either way." value={session.data.preferences.omit_private_tool_payloads ?? false} />
      </div>
    </section>
  </section>
}
