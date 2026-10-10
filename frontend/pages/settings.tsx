import { useEffect, useRef, useState } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import { queryClient, unwrap } from "../api/client"
import { queries } from "../api/queries"
import { getSessionTitleConfiguration, updateChatPreferences, updateSessionTitleConfiguration } from "../api/generated/sdk.gen"
import type { ChatPreferences, SessionTitleConfiguration, WorkspaceSession } from "../api/generated/types.gen"
import { Button } from "../components/ui/button"
import { PreferenceSwitch } from "../blocks/settings/preference-switch"
import { SettingsOverview } from "../blocks/settings/settings-overview"
import { TitleModelForm } from "../blocks/settings/title-model-form"
import { settingsGroups } from "../config/settings-navigation"
import { currentUserId, useIdentity } from "../legacy/runtime"

const preferences: { name: keyof ChatPreferences; title: string; description: string }[] = [
  { name: "send_immediately", title: "Send messages immediately", description: "Send follow-ups into the active response instead of queueing them while Moyai is working." },
  { name: "omit_private_tool_payloads", title: "Hide private tool content in traces", description: "Hide inputs and results from credential, skill, memory, and connector tools in new responses. Secret redaction stays on either way." },
]

function titleHelp(titles: SessionTitleConfiguration | undefined, admin: boolean) {
  if (!titles) return "Loading…"
  if (!titles.enabled) return "Title generation is disabled by the server."
  if (!titles.gateway_configured) return "Gateway access must be configured on the server."
  return admin ? "Enter the exact model ID enabled on your gateway." : "Only administrators can change the workspace title model."
}

function Preference({ name, title, description, value }: { name: keyof ChatPreferences; title: string; description: string; value: boolean }) {
  const { userId } = useIdentity()
  const [status, setStatus] = useState("")
  const save = useMutation({
    mutationFn: async (checked: boolean) => unwrap(await updateChatPreferences({ body: { [name]: checked } })),
    onSuccess(saved) {
      if (currentUserId() !== userId) return
      const changed = { [name]: saved[name] }
      // Updating the session query also syncs the controllers' preferences.
      queryClient.setQueryData(queries.session.current(userId).queryKey, (current: WorkspaceSession | undefined) => current && { ...current, preferences: { ...current.preferences, ...changed } })
      setStatus(" · Saved")
    },
    onError(error) { setStatus(` · Could not confirm the change. ${error.message} Reload settings to check, or try again.`) },
  })
  return <PreferenceSwitch id={name.replaceAll("_", "-")} title={title} description={description} checked={value} pending={save.isPending}
    status={save.isPending ? "Saving…" : `${value ? "On" : "Off"}${status}`} onCheckedChange={checked => { setStatus(""); save.mutate(checked) }} />
}

export function SettingsPage() {
  const { userId } = useIdentity()
  const session = useQuery(queries.session.current(userId))
  const titles = useQuery(queries.sessionTitles.configuration())
  const [model, setModel] = useState("")
  const [titleStatus, setTitleStatus] = useState("")
  const heading = useRef<HTMLHeadingElement>(null)
  const admin = session.data?.role === "admin"
  useEffect(() => { if (titles.data) setModel(titles.data.model) }, [titles.data])
  useEffect(() => { heading.current?.focus({ preventScroll: true }) }, [session.isSuccess])
  const saveTitle = useMutation({
    mutationFn: async () => {
      unwrap(await updateSessionTitleConfiguration({ body: { model: model.trim() } }))
      return unwrap(await getSessionTitleConfiguration())
    },
    onSuccess(saved) {
      queryClient.setQueryData(queries.sessionTitles.configuration().queryKey, saved)
      setModel(saved.model)
      setTitleStatus(`Saved. New title attempts use ${saved.model}.`)
    },
    onError(error) { setTitleStatus(error.message) },
  })
  if (session.isPending) return <p className="subtext" role="status">Loading settings…</p>
  if (session.isError) return <section className="settings-load-error"><h1>Settings</h1><div role="alert"><h2>Unable to load this page</h2><p>{session.error.message}</p><p>Check your connection and try again.</p></div><Button id="settings-retry" onClick={() => void session.refetch()}>Try again</Button></section>
  if (!session.data.authenticated) return null
  return <section className="settings-page">
    <div className="page-heading"><div><h1 ref={heading} tabIndex={-1}>Settings</h1><p className="subtext">Make Moyai work the way your team does.</p></div></div>
    <SettingsOverview groups={settingsGroups} role={session.data.role ?? "member"} />
    <TitleModelForm model={model} editable={admin && !titles.isPending} pending={saveTitle.isPending} status={titleStatus || titles.error?.message || titleHelp(titles.data, admin)}
      onModelChange={setModel} onSubmit={() => { setTitleStatus(""); saveTitle.mutate() }} />
    <section className="settings-group" aria-labelledby="preferences-title">
      <div><h2 id="preferences-title">Preferences</h2><p className="subtext">Saved for your account.{!session.data.identity && " Shared password and local sign-ins use a shared profile."}</p></div>
      <div className="settings-preferences">{preferences.map(item => <Preference key={item.name} {...item} value={session.data.preferences[item.name] ?? false} />)}</div>
    </section>
  </section>
}
