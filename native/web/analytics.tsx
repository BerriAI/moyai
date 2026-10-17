import React, { useEffect, useMemo, useState } from "react"
import { ArrowDown, ArrowUp, ArrowUpDown, Download, RefreshCw, Search } from "lucide-react"
import { Button } from "@/components/ui/button"
import { Input } from "@/components/ui/input"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table"
import { api, ApiError } from "./api"
import { AnalyticsChart, AnalyticsDatePicker, AnalyticsLegend, AnalyticsMetrics, ReportTable } from "./analytics-shared"
import { StartupPerformance } from "./performance"
import { timingReason, timingSource } from "./performance-labels"
import { presetAnalyticsRange } from "./analytics-dates"
import type { AnalyticsRange } from "./analytics-dates"
import { dollars, elapsed, finiteValue, spendCount } from "./analytics-format"
import { combinedUserRows, modelHistory, partialCost, prStates, reportedCostPerMerge, sortUserRows, validPRDistribution } from "./analytics-data"
import type { AnalyticsReport, ReportSummary, ReportUser, UserSort, UserSortKey } from "./analytics-data"
import "./analytics.css"

type ReportKind = "usage" | "performance"
type ReportView = "overall" | "users" | "history"
type Filters = { model: string; status: string; user_id: string }
const allFilters: Filters = { model: "all", status: "all", user_id: "all" }
const views = [{ id: "overall", label: "Overall" }, { id: "users", label: "Users" }, { id: "history", label: "Usage history" }] as const
const statuses = ["queued", "starting", "running", "waiting", "saving", "steering", "signal_sent", "accepted", "included", "unconfirmed", "resumed", "completed", "failed", "stopped"]
const localDate = (value: string) => Number.isFinite(Date.parse(value)) ? new Date(value).toLocaleString() : "Not recorded"

function queryFor(range: AnalyticsRange, filters: Filters) {
  const params = new URLSearchParams(range)
  for (const [key, value] of Object.entries(filters)) if (value !== "all") params.set(key, value)
  return params
}

function useReport(path: string, revision: number, billing: boolean) {
  const [state, setState] = useState<{ path: string; data?: AnalyticsReport; error?: string; denied?: boolean; loading: boolean }>({ path, loading: true })
  useEffect(() => {
    const controller = new AbortController()
    setState(previous => ({ path, data: previous.path === path ? previous.data : undefined, loading: true }))
    void api<AnalyticsReport | { activity: AnalyticsReport }>(path, "GET", undefined, controller.signal).then(data => {
      const report = billing ? "activity" in data ? data.activity : undefined : "summary" in data ? data : undefined
      if (!report) throw new Error("The usage report is unavailable.")
      if (!controller.signal.aborted) setState({ path, data: report, loading: false })
    }).catch(error => {
      if (!controller.signal.aborted) setState({
        path, loading: false, denied: error instanceof ApiError && [401, 403].includes(error.status),
        error: error instanceof Error ? error.message : "The report could not be loaded.",
      })
    })
    return () => controller.abort()
  }, [path, revision, billing])
  return state.path === path ? state : { path, loading: true }
}

function ReportSelect({ label, value, onChange, options }: { label: string; value: string; onChange: (value: string) => void; options: { value: string; label: string }[] }) {
  return <Select value={value} onValueChange={onChange}><SelectTrigger aria-label={label} title={options.find(item => item.value === value)?.label}><SelectValue /></SelectTrigger><SelectContent className="native-settings-select">{options.map(option => <SelectItem key={option.value} value={option.value}>{option.label}</SelectItem>)}</SelectContent></Select>
}

function Cost({ row, compact = false }: { row: { cost_usd?: number | null; cost_usd_exact?: string | null; pricing_status?: string; unpriced_requests?: number; unpriced_turns?: number; unknown_request_turns?: number; capture_gap_turns?: number; cost_coverage?: number | null }; compact?: boolean }) {
  return <><span title={row.cost_usd_exact ? `Exact reported USD: ${row.cost_usd_exact}` : undefined}>{dollars(row.cost_usd)}</span>{finiteValue(row.cost_usd) && partialCost(row) && <small className="analytics-warning" title="Partial reported subtotal; missing costs are excluded.">{compact ? "Partial" : "Incomplete"}</small>}</>
}

function CostCoverage({ data }: { data: AnalyticsReport }) {
  const coverage = data.coverage
  const incomplete = partialCost(data.summary) || coverage?.unknown_requests || coverage?.unknown_request_turns
    || coverage?.capture_gap_turns || coverage?.activity_unknown_request_turns || coverage?.activity_capture_gap_turns
  return incomplete ? <p className="analytics-status analytics-warning" role="status">Spend is incomplete; missing costs are excluded.</p> : null
}

function PRDistribution({ row }: { row: Partial<ReportSummary> }) {
  if (!validPRDistribution(row.pr_status_counts, row.prs_created)) return <span className="analytics-muted">Unavailable</span>
  if (!row.prs_created) return <span className="analytics-muted">No creations</span>
  return <div className="analytics-pr-distribution" role="group" aria-label="Statuses of PRs created in the selected range">
    <div className="analytics-pr-status-bar" aria-hidden="true">{prStates.map(state => <i key={state.key} className={`pr-distribution-${state.key}`} style={{ width: `${row.pr_status_counts![state.key] / row.prs_created! * 100}%` }} />)}</div>
    <div className="analytics-pr-status-counts">{prStates.filter(state => row.pr_status_counts![state.key] > 0).map(state => <span key={state.key}>{state.label} {spendCount(row.pr_status_counts![state.key])}</span>)}</div>
  </div>
}

function PRNotice({ data }: { data: AnalyticsReport }) {
  const coverage = data.coverage
  const incomplete = coverage?.pr_status_unknown || coverage?.pr_merge_date_unknown || coverage?.unclassified_confirmed_create_calls
  return incomplete ? <p className="analytics-status analytics-warning">Some PR data is unavailable.</p> : null
}

function OverallReport({ data }: { data: AnalyticsReport }) {
  const activeKey = data.series.some(day => day.sessions_active !== undefined) ? "sessions_active" : "sessions"
  const sessionSeries = [{ key: activeKey, label: "Active sessions" }]
  const sessionLine = { key: "sessions_created", label: "Created sessions", line: true }
  const prSeries = [{ key: "prs_created", label: "Created" }]
  const prLine = { key: "prs_merged", label: "Merged in range", line: true }
  return <>
    <AnalyticsMetrics items={[
      ["Model spend", dollars(data.summary.cost_usd)],
      ["Requests", spendCount(data.summary.requests), data.summary.unknown_requests || data.summary.unknown_request_turns ? "Incomplete" : undefined],
      ["Tokens", spendCount(data.summary.total_tokens), finiteValue(data.summary.token_coverage) && data.summary.token_coverage < 1 ? "Incomplete" : undefined],
    ]} /><CostCoverage data={data} />
    <section className="analytics-section"><h2>Sessions</h2><AnalyticsMetrics items={[
      ["Created", spendCount(data.summary.sessions_created)], ["Active", spendCount(data.summary.sessions_active ?? data.summary.sessions)],
    ]} /><div className="analytics-chart-heading"><h3>Sessions over time</h3><span>Daily · UTC</span></div><AnalyticsLegend series={[...sessionSeries, sessionLine]} />
      <AnalyticsChart rows={data.series.map(day => ({ date: day.date, sessions: day.sessions, sessions_active: day.sessions_active, sessions_created: day.sessions_created }))} series={sessionSeries} line={sessionLine} title="Sessions over time" />
    </section>
    <section className="analytics-section"><h2>Pull requests</h2>{data.capabilities?.pr_analytics ? <>
      <AnalyticsMetrics items={[
        ["Created", spendCount(data.summary.prs_created)],
        ["Merged", spendCount(data.summary.prs_merged)],
        ["Unknown status", spendCount(data.summary.pr_status_counts?.unknown ?? data.summary.prs_unknown)],
      ]} /><AnalyticsLegend series={[...prSeries, prLine]} /><AnalyticsChart rows={data.series.map(day => ({ date: day.date, prs_created: day.prs_created, prs_merged: day.prs_merged }))} series={prSeries} line={prLine} title="PR creation and merge events" /><PRNotice data={data} />
    </> : <p className="analytics-status">PR analytics are not available from this server.</p>}</section>
    <section className="analytics-section"><h2>Model usage</h2><div className="analytics-chart-heading"><h3>Model requests over time</h3><span>Daily · UTC</span></div>
      <AnalyticsChart rows={data.series.map(day => ({ date: day.date, requests: day.requests }))} series={[{ key: "requests", label: "Model requests" }]} title="Model requests over time" area />
    </section>
  </>
}

const userColumns: { key?: UserSortKey; label: string; note?: string }[] = [
  { key: "user", label: "User" }, { key: "sessions", label: "Sessions" }, { key: "turns", label: "Turns" },
  { key: "requests", label: "Requests" }, { key: "tokens", label: "Tokens" },
  { key: "spend", label: "Reported spend" }, { key: "prs_created", label: "PRs created" },
  { label: "PR statuses", note: "Created in range" }, { key: "prs_merged", label: "PRs merged", note: "Merged in range" },
  { key: "cost_per_merge", label: "$ / merged PR" },
]

export function UsersReport({ data, search, onSearch, sort, onSort }: {
  data: AnalyticsReport; search: string; onSearch: (value: string) => void; sort: UserSort; onSort: (value: UserSort) => void
}) {
  const allRows = useMemo(() => combinedUserRows(data.by_user ?? [], data.users), [data.by_user, data.users])
  const prAvailable = data.capabilities?.pr_analytics === true
  const rows = useMemo(() => sortUserRows(allRows.filter(row =>
    row.search.includes(search.trim().toLocaleLowerCase())), sort, prAvailable), [allRows, search, sort, prAvailable])
  function changeSort(key: UserSortKey) {
    onSort({ key, direction: sort.key === key ? sort.direction === "asc" ? "desc" : "asc" : key === "user" ? "asc" : "desc" })
  }
  return <>
    <section className="analytics-section analytics-users">
      <div className="analytics-section-heading"><h2>Users</h2>
        <div className="analytics-user-search"><Search size={16} /><Input type="search" aria-label="Search report users" placeholder="Search users…" value={search} onChange={event => onSearch(event.target.value)} />
          {search && <Button variant="ghost" size="sm" onClick={() => onSearch("")}>Clear</Button>}
        </div>
      </div>
      {!data.by_user ? <p className="analytics-status">User aggregates are not available from this server.</p>
        : !rows.length ? <p className="analytics-status" role="status">{search ? "No matching users. Clear the search to see all users." : "No recorded user activity in this range."}</p>
          : <div className="analytics-table-frame analytics-users-table" role="region" aria-label="Usage and PR outcomes by user" tabIndex={0}>
            <Table><TableHeader><TableRow>{userColumns.map(column => <TableHead key={column.label} scope="col"
              aria-sort={column.key ? sort.key === column.key ? sort.direction === "asc" ? "ascending" : "descending" : "none" : undefined}>
              {column.key ? <Button variant="ghost" size="sm" className="analytics-user-sort" aria-label={`Sort by ${column.label}`}
                onClick={() => changeSort(column.key!)}>
                <span>{column.label}</span>{sort.key !== column.key ? <ArrowUpDown size={12} /> : sort.direction === "asc" ? <ArrowUp size={12} /> : <ArrowDown size={12} />}
              </Button> : column.label}{column.note && <small>{column.note}</small>}
            </TableHead>)}</TableRow></TableHeader><TableBody>
              {rows.map(({ user_id, label, secondary, metrics: row }) => <TableRow key={user_id}>
                <TableCell><strong>{label}</strong>{secondary && <small>{secondary}</small>}</TableCell>
                <TableCell>{spendCount(row.sessions_active ?? row.sessions)}</TableCell>
                <TableCell>{spendCount(row.turns)}</TableCell>
                <TableCell>{spendCount(row.requests)}{Boolean(row.unknown_request_turns) && <small title="These turns have unavailable request counts.">{spendCount(row.unknown_request_turns)} {row.unknown_request_turns === 1 ? "unknown turn" : "unknown turns"}</small>}</TableCell>
                <TableCell>{spendCount(row.total_tokens)}
                  {finiteValue(row.total_tokens) && finiteValue(row.token_coverage) && row.token_coverage < 1 && <small className="analytics-warning">Partial</small>}
                </TableCell>
                <TableCell><Cost row={row} compact /></TableCell>
                <TableCell>{spendCount(prAvailable ? row.prs_created : null)}</TableCell>
                <TableCell>{prAvailable ? <PRDistribution row={row} /> : <span className="analytics-muted">Unavailable</span>}</TableCell>
                <TableCell>{spendCount(prAvailable ? row.prs_merged : null)}</TableCell>
                <TableCell>{dollars(prAvailable ? reportedCostPerMerge(row) : null)}
                  {prAvailable && reportedCostPerMerge(row) === null && <small>{row.prs_merged === 0 ? "No merges" : finiteValue(row.prs_merged) ? "Coverage incomplete" : "PR data unavailable"}</small>}
                </TableCell>
              </TableRow>)}
            </TableBody></Table>
          </div>}
      {data.by_user && <p className="analytics-users-note analytics-muted">{rows.length} of {allRows.length} users.</p>}
      <details className="analytics-methodology analytics-users-methodology"><summary>Coverage and calculations</summary>
        <CostCoverage data={data} />{prAvailable && <PRNotice data={data} />}
        <p>Cost per merged PR uses spend from sessions linked to merged PRs. Incomplete values are unavailable.</p>
        <p>PRs count toward their creation or merge date. Dates use UTC.</p>
        {data.capabilities?.unified_billing && <p>Spend includes all billing history; activity counts cover current sessions.</p>}
      </details>
    </section>
  </>
}

function UsageHistory({ data, exportUrl }: { data: AnalyticsReport; exportUrl: (format: string) => string }) {
  const history = useMemo(() => modelHistory(data), [data])
  return <>
    <section className="analytics-section"><h2>Usage history</h2><div className="analytics-history-card"><span className="analytics-muted">Model spend</span><strong className="analytics-total">{dollars(data.summary.cost_usd)}</strong>
      {data.by_model ? <>
        <AnalyticsLegend series={history.series} cumulative={history.cumulative} />
        <AnalyticsChart rows={history.rows} series={history.series} title="Daily reported spend by model and cumulative subtotal" money cumulative={history.cumulative} cumulativeKey="cumulative_cost_usd" />
        {!history.cumulative && history.rows.some(row => finiteValue(row.cumulative_cost_usd)) && <p className="analytics-warning analytics-footnote">The cumulative series is unavailable because its reported values do not match the model breakdown.</p>}
      </> : <p className="analytics-status">Per-model cost history is not available from this server.</p>}
    </div><CostCoverage data={data} /></section>
    <section className="analytics-section"><div className="analytics-section-heading"><h2>Models</h2>{data.capabilities?.reported_costs && <Button asChild variant="ghost" size="sm"><a href={exportUrl("models")} download><Download size={14} />Export models</a></Button>}</div>
      {history.models.length ? <ReportTable label="Model usage and reported spend" columns={["Inference model", "Requests", "Tokens", "Reported spend", "Coverage"]}>{history.models.map(row => <TableRow key={row.model}><TableCell>{row.model}</TableCell><TableCell>{spendCount(row.requests)}</TableCell><TableCell>{spendCount(row.total_tokens)}<small>{spendCount(row.input_tokens)} in · {spendCount(row.output_tokens)} out</small></TableCell><TableCell><Cost row={row} /></TableCell><TableCell>{spendCount(row.priced_requests)} priced<small>{spendCount(row.unpriced_requests)} unpriced · {row.pricing_status}</small></TableCell></TableRow>)}</ReportTable>
        : <p className="analytics-status">No per-model usage records in this range.</p>}
    </section>
    <details className="analytics-breakdown"><summary>Daily usage and cost breakdown</summary><ReportTable label="Daily usage and cost breakdown" columns={["Date (UTC)", "Requests", "Tokens", "Unpriced requests", "Reported cost", "Cumulative subtotal"]}>
      {data.series.map(day => <TableRow key={day.date}><TableCell>{day.date}</TableCell><TableCell>{spendCount(day.requests)}</TableCell><TableCell>{spendCount(day.total_tokens)}</TableCell><TableCell>{spendCount(day.unpriced_requests)}</TableCell><TableCell><Cost row={day} /></TableCell><TableCell>{dollars(day.cumulative_cost_usd)}{day.cumulative_pricing_status === "partial" && <small className="analytics-warning">Partial</small>}</TableCell></TableRow>)}
    </ReportTable></details>
  </>
}

function TurnTable({ data, performance, offset, onOffset, loading }: { data: AnalyticsReport; performance: boolean; offset: number; onOffset: (offset: number) => void; loading: boolean }) {
  return <>
    <ReportTable label={performance ? "Recorded turn performance" : "Recorded turn usage"} columns={["Turn", "User", "Model / status", ...(performance ? ["Startup", "Startup breakdown", "Computer ready", "Duration"] : ["Requests", "Tokens", "Reported cost"])]}>
      {data.rows.map(row => <TableRow key={row.id}>
        <TableCell><details><summary>{row.id.slice(0, 8)}</summary><code>{row.id}</code><small>Tool calls: {spendCount(row.tool_calls)}</small><small>Cache read: {spendCount(row.cache_read_tokens)} · write: {spendCount(row.cache_write_tokens)}</small>{row.usage_role === "steering_input" && <small>Steering input; inference cost belongs to its parent turn.</small>}{Boolean(row.cost_models?.length) && <small>Inference models: {row.cost_models!.join(", ")}</small>}</details><small>{localDate(row.created_at)}</small></TableCell>
        <TableCell>{data.users.find(user => user.id === row.user_id)?.display_name || data.users.find(user => user.id === row.user_id)?.username || row.user_id}</TableCell><TableCell>{row.model}<small>{row.status}</small></TableCell>
        {performance ? <>
          <TableCell>{elapsed(row.api_to_dispatch_ms)}<small>{timingReason(row.startup_timing_status)}</small>{(row.api_received_at || row.first_dispatch_at) && <details><summary>Timing evidence</summary><small>Start: {timingSource(row.timing_start_source)}{row.timing_surface ? ` · ${row.timing_surface}` : ""}</small><small>Receipt: {row.api_received_at || "Unavailable"}</small><small>Worker: {row.worker_picked_up_at || "Unavailable"}</small><small>Broker: {row.broker_received_at || "Unavailable"}</small><small>Dispatch: {row.first_dispatch_at || "Unavailable"}</small><small>Route: {row.dispatch_route || "Unavailable"}</small><code>{row.dispatch_attempt_id}</code></details>}</TableCell>
          <TableCell><small>Receipt → worker: {elapsed(row.queue_pickup_ms ?? row.api_to_worker_ms)}</small>{row.queue_timing_status !== "measured" && <small>{timingReason(row.queue_timing_status)}</small>}<small>Worker → broker: {elapsed(row.worker_to_broker_ms)}</small><small>Broker → dispatch: {elapsed(row.broker_preparation_ms ?? row.broker_to_dispatch_ms)}</small>{row.broker_timing_status !== "measured" && <small>{timingReason(row.broker_timing_status)}</small>}</TableCell>
          <TableCell>{elapsed(row.computer_ready_ms)}<small>{row.computer_mode}{row.computer_status ? ` · ${row.computer_status}` : ""}</small><small>{timingReason(row.computer_timing_status)}</small></TableCell>
          <TableCell>{elapsed(row.total_ms)}<small>Accepted → finished: {elapsed(row.latency_ms)}</small></TableCell>
        </> : <><TableCell>{spendCount(row.requests)}</TableCell><TableCell>{spendCount(row.total_tokens)}</TableCell><TableCell>{dollars(row.cost_usd)}<small>{row.pricing_status}</small></TableCell></>}
      </TableRow>)}
    </ReportTable>
    {!data.rows.length && <p className="analytics-status">{data.coverage?.private_activity_details_omitted ? "Private turn activity is included in aggregates; its details are not visible to this account." : "No turns match these filters."}</p>}
    <div className="analytics-pagination"><p role="status">{data.total ? `${spendCount(data.offset + 1)}–${spendCount(Math.min(data.offset + data.rows.length, data.total))} of ${spendCount(data.total)} turns` : "No turns in this range"}</p><div><Button variant="outline" disabled={loading || offset === 0} onClick={() => onOffset(Math.max(0, data.offset - data.limit))}>Previous</Button><Button variant="outline" disabled={loading || data.offset + data.limit >= data.total} onClick={() => onOffset(data.offset + data.limit)}>Next</Button></div></div>
  </>
}

function PerformanceReport({ data }: { data: AnalyticsReport }) {
  const series = [{ key: "mean_latency_ms", label: "Mean latency (ms)" }], line = { key: "p95_latency_ms", label: "p95 latency (ms)", line: true }
  return <>
    <StartupPerformance data={data.performance} />
    <details className="analytics-breakdown">
    <summary>Turn duration</summary>
    <p className="analytics-status">Includes model work, tools, and waits.</p>
    <AnalyticsMetrics items={[
      ["Mean turn latency", elapsed(data.summary.mean_latency_ms)], ["Median (p50)", elapsed(data.summary.p50_latency_ms)],
      ["p95", elapsed(data.summary.p95_latency_ms)], ["Measured turns", `${spendCount(data.summary.latency_samples)} / ${spendCount(data.summary.turns)}`, "Missing timings are excluded"],
    ]} /><section className="analytics-section"><div className="analytics-chart-heading"><h2>Turn latency over time</h2><span>Milliseconds · UTC</span></div><AnalyticsLegend series={[...series, line]} />
      <AnalyticsChart rows={data.series.map(day => ({ date: day.date, mean_latency_ms: day.mean_latency_ms, p95_latency_ms: day.p95_latency_ms }))} series={series} line={line} title="Turn latency in milliseconds" area />
    </section>
    </details>
  </>
}

export function AnalyticsPage({ kind, productMode, headingRef }: { kind: ReportKind; productMode?: "standalone" | "existing"; headingRef?: React.Ref<HTMLHeadingElement> }) {
  const [range, setRange] = useState<AnalyticsRange>(() => presetAnalyticsRange())
  const [filters, setFilters] = useState<Filters>(allFilters), [view, setView] = useState<ReportView>("overall")
  const [offset, setOffset] = useState(0), [revision, setRevision] = useState(0), [search, setSearch] = useState("")
  const [userSort, setUserSort] = useState<UserSort>({ key: "spend", direction: "desc" })
  const [modelOptions, setModelOptions] = useState<string[]>([]), [userOptions, setUserOptions] = useState<ReportUser[]>([])
  const params = queryFor(range, filters)
  const billing = kind === "usage" && productMode === "existing"
  if (billing) {
    if (params.has("model")) { params.set("root_model", params.get("model")!); params.delete("model") }
    if (params.has("status")) { params.set("turn_status", params.get("status")!); params.delete("status") }
    params.set("include_activity", "true")
  }
  const endpoint = billing ? "/settings/billing" : `/settings/${kind}`
  const resource = useReport(`${endpoint}?${params}&limit=50&offset=${offset}`, revision, billing)
  useEffect(() => {
    if (resource.denied) { setModelOptions([]); setUserOptions([]) }
    else if (resource.data) {
      setModelOptions(previous => [...new Set([...previous, ...resource.data!.models])].sort())
      setUserOptions(resource.data.users)
    }
  }, [resource.data, resource.denied])
  const refresh = () => { if (range.preset !== "custom") setRange(presetAnalyticsRange(range.preset)); setRevision(value => value + 1) }
  const updateFilter = (key: keyof Filters, value: string) => { setFilters(previous => ({ ...previous, [key]: value })); setOffset(0) }
  const aggregateExports = resource.data?.capabilities?.reported_costs === true
  const exportKind = kind === "performance" || !aggregateExports ? "turns" : view === "users" ? "users" : "daily"
  const exportUrl = (format: string) => `/api${endpoint}?${params}&format=csv&export=${encodeURIComponent(format)}`
  return <div className="native-analytics">
    <header className="ns-page-heading analytics-report-heading"><h1 ref={headingRef} tabIndex={-1}>{kind === "performance" ? "Performance" : "Spend & usage"}</h1>
      <div className="analytics-actions"><AnalyticsDatePicker value={range} onChange={value => { setRange(value); setOffset(0) }} /><Button variant="outline" disabled={resource.loading} onClick={refresh}><RefreshCw size={16} />Refresh</Button></div>
    </header>
    <div className="analytics-toolbar">{kind === "usage" ? <div className="analytics-tabs" role="tablist" aria-label="Spend and usage view">{views.map((item, index) => <Button key={item.id} id={`analytics-tab-${item.id}`} type="button" variant="ghost" role="tab" aria-selected={view === item.id} aria-controls="analytics-report-panel" tabIndex={view === item.id ? 0 : -1} onClick={() => setView(item.id)} onKeyDown={event => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return
      event.preventDefault()
      const next = event.key === "Home" ? 0 : event.key === "End" ? views.length - 1 : (index + (event.key === "ArrowRight" ? 1 : -1) + views.length) % views.length
      setView(views[next].id); event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
    }}>{item.label}</Button>)}</div> : null}
      {resource.data && !resource.loading && !resource.error ? <Button asChild className="analytics-export"><a href={exportUrl(exportKind)} download><Download size={16} />{aggregateExports || kind === "performance" ? "Export CSV" : "Export turns CSV"}</a></Button> : <Button className="analytics-export" disabled><Download size={16} />Export CSV</Button>}
    </div>
    <div className="analytics-filters"><ReportSelect label="Model" value={filters.model} onChange={value => updateFilter("model", value)} options={[{ value: "all", label: "All models" }, ...modelOptions.map(value => ({ value, label: value }))]} />
      <ReportSelect label="User" value={filters.user_id} onChange={value => updateFilter("user_id", value)} options={[{ value: "all", label: "All users" }, ...userOptions.filter(row => row.filterable !== false).map(row => ({ value: row.id, label: row.display_name || row.username }))]} />
      <ReportSelect label="Status" value={filters.status} onChange={value => updateFilter("status", value)} options={[{ value: "all", label: "All statuses" }, ...statuses.map(value => ({ value, label: (value[0].toUpperCase() + value.slice(1)).replaceAll("_", " ") }))]} />
      {Object.values(filters).some(value => value !== "all") && <Button variant="ghost" onClick={() => { setFilters(allFilters); setOffset(0) }}>Clear filters</Button>}
    </div>
    <div id="analytics-report-panel" role={kind === "usage" ? "tabpanel" : undefined} aria-labelledby={kind === "usage" ? `analytics-tab-${view}` : undefined} aria-busy={resource.loading}>
      {resource.error ? <div className="ns-empty" role="alert"><h2>Unable to load this report</h2><p>{resource.error}</p><Button variant="outline" onClick={refresh}>Try again</Button></div>
        : !resource.data ? <p className="analytics-status" role="status">Loading report…</p> : <>
          {kind === "performance" ? <PerformanceReport data={resource.data} /> : view === "overall" ? <OverallReport data={resource.data} /> : view === "users" ? <UsersReport data={resource.data} search={search} onSearch={setSearch} sort={userSort} onSort={setUserSort} /> : <UsageHistory data={resource.data} exportUrl={exportUrl} />}
          {kind === "performance" ? <section className="analytics-section"><h2>Turns</h2><TurnTable data={resource.data} performance offset={offset} onOffset={setOffset} loading={resource.loading} /></section>
            : view === "history" && <details className="analytics-breakdown"><summary>{resource.data.capabilities?.unified_billing ? "Turn details" : "Turns"}</summary><Button asChild variant="ghost" size="sm"><a href={exportUrl("turns")} download><Download size={14} />Export turns</a></Button><TurnTable data={resource.data} performance={false} offset={offset} onOffset={setOffset} loading={resource.loading} /></details>}
          {(kind !== "usage" || view !== "users") && <details className="analytics-methodology"><summary>About this data</summary>
            <p>Dates use UTC. Missing values are unavailable, not zero.</p>
            {kind === "usage" ? <><p>Spend includes model charges; infrastructure costs are excluded. PRs count toward their creation or merge date.</p>
              {resource.data.capabilities?.unified_billing && <p>Spend includes all billing history; activity counts cover current sessions.</p>}</>
              : <p>Startup ends when the first model request is sent. Turn duration includes model work, tools, and waits.</p>}
          </details>}
        </>}
    </div>
  </div>
}
