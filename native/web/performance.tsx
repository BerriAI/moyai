import { Card } from "@/components/ui/card"
import { TableCell, TableRow } from "@/components/ui/table"
import type { NativePerformance, TimingCard } from "./analytics-data"
import { elapsed, finiteValue, spendCount } from "./analytics-format"
import { ReportTable } from "./analytics-shared"
import { PerformanceTrend, timingSeries } from "./performance-trend"
import { timingReason } from "./performance-labels"

function PerformanceCard({ title, description, metric, id }: {
  title: string; description: string; metric: TimingCard; id: string
}) {
  const duration = (value: unknown) => finiteValue(value) ? elapsed(value) : "—"
  return <Card className="performance-card" role="region" aria-labelledby={id}>
    <h2 id={id}>{title}</h2><p className="performance-description">{description}</p>
    <p className="performance-average" title={finiteValue(metric.mean_ms) ? undefined : "Not measured"}>{duration(metric.mean_ms)}<span>average</span></p>
    <dl className="performance-percentiles">
      <div><dt>Median</dt><dd title={finiteValue(metric.p50_ms) ? undefined : "Not measured"}>{duration(metric.p50_ms)}</dd></div>
      <div><dt>p95</dt><dd title={finiteValue(metric.p95_ms) ? undefined : "Not measured"}>{duration(metric.p95_ms)}</dd></div>
      <div><dt>Slowest</dt><dd title={finiteValue(metric.max_ms) ? undefined : "Not measured"}>{duration(metric.max_ms)}</dd></div>
    </dl>
    <p className="performance-coverage">{spendCount(metric.samples)} measured · {spendCount(metric.pending)} waiting</p>
    {(metric.missing > 0 || Boolean(metric.excluded)) && <p className="performance-coverage">{spendCount(metric.missing)} unavailable{Boolean(metric.excluded) && <> · {spendCount(metric.excluded)} excluded from average</>}</p>}
    {metric.reasons && <details className="performance-coverage"><summary>Timing coverage · {spendCount(metric.total)} {metric.total === 1 ? "turn" : "turns"}</summary>
      <ul>{Object.entries(metric.reasons).filter(([, count]) => count > 0).map(([reason, count]) =>
        <li key={reason}>{spendCount(count)} · {timingReason(reason)}</li>)}</ul>
    </details>}
  </Card>
}

export function StartupPerformance({ data }: { data?: NativePerformance }) {
  if (!data) return <p className="analytics-status" role="status">Startup measurements are unavailable from this server. The native API and execution workers need the performance instrumentation release.</p>
  const { dispatch, breakdown, computer } = data
  return <>
    {data.accounted_turns !== undefined && <p className="analytics-status"><strong>{spendCount(data.accounted_turns)} {data.accounted_turns === 1 ? "turn" : "turns"} accounted for.</strong> Every turn admitted in this range has a timing status. Expand Timing coverage for the reasons. Older timestamps are not reconstructed.</p>}
    {data.cards ? <div className="performance-metrics">
      {timingSeries.map(item => <PerformanceCard key={item.key} title={item.title} description={item.description} metric={data.cards![item.key]} id={`performance-${item.key}`} />)}
    </div> : <p className="analytics-status">The four-card report is unavailable from this server. Update the native API to load its measurements.</p>}
    {!dispatch.samples && <p className="analytics-status" role="status">No complete startup measurements in this range yet. New turns acquire timings once the updated API, execution worker and broker are running. Older records are not backfilled.</p>}
    <PerformanceTrend rows={data.trend} />
    <section className="analytics-section" aria-labelledby="startup-timing-title">
      <h2 id="startup-timing-title">Startup breakdown</h2>
      <p className="analytics-status">Component means use the same {spendCount(breakdown.samples)} complete root turns and add to the total below.</p>
      <ReportTable label="Startup breakdown" columns={["Stage", "Mean", "What it includes"]}>
        <TableRow><TableCell>Receipt → execution worker</TableCell><TableCell>{elapsed(breakdown.api_to_worker_ms.mean_ms)}</TableCell><TableCell>Admission, queue wait and worker pickup; HTTP timings also include authentication</TableCell></TableRow>
        <TableRow><TableCell>Execution worker → broker</TableCell><TableCell>{elapsed(breakdown.worker_to_broker_ms.mean_ms)}</TableCell><TableCell>Context, workspace preparation when needed, and broker handoff</TableCell></TableRow>
        <TableRow><TableCell>Broker → LiteLLM dispatch</TableCell><TableCell>{elapsed(breakdown.broker_to_dispatch_ms.mean_ms)}</TableCell><TableCell>Authorization, capacity wait and request admission</TableCell></TableRow>
        <TableRow><TableCell><strong>Total for this cohort</strong></TableCell><TableCell><strong>{elapsed(breakdown.api_to_dispatch_ms.mean_ms)}</strong></TableCell><TableCell>{spendCount(breakdown.samples)} complete measurements</TableCell></TableRow>
      </ReportTable>
      <p className="analytics-muted">Web messages and approval replies start at HTTP receipt. Slack, scheduled and internal turns start at native admission; their upstream delivery time is excluded. Each turn identifies its source. This measures the outbound attempt. Direct local execution uses local inference admission as the broker boundary.</p>
    </section>
    <section className="analytics-section" aria-labelledby="computer-timing-title">
      <h2 id="computer-timing-title">First computer readiness</h2>
      <p className="analytics-status">Computer acquisition is measured separately because the model can respond before it needs a sandbox. It includes workspace preparation and ends when the SDK’s workspace check succeeds.</p>
      <ReportTable label="Computer readiness by acquisition mode" columns={["Acquisition", "Ready samples", "Mean", "p50", "p95"]}>
        {([["cold", "Cold start"], ["restore", "Snapshot restore"], ["reuse", "Existing computer"]] as const).map(([mode, label]) => {
          const item = computer[mode]
          return <TableRow key={mode}><TableCell>{label}</TableCell><TableCell>{spendCount(item.samples)}</TableCell><TableCell>{elapsed(item.mean_ms)}</TableCell><TableCell>{elapsed(item.p50_ms)}</TableCell><TableCell>{elapsed(item.p95_ms)}</TableCell></TableRow>
        })}
      </ReportTable>
      <p className="analytics-status">{spendCount(computer.requested)} first acquisition attempts · {spendCount(computer.failed)} failed or cancelled · {spendCount(computer.pending)} without a completion record. Turns without computer evidence are excluded.</p>
    </section>
  </>
}
