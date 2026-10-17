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
    <h2 id={id} title={description}>{title}</h2>
    <p className="performance-average" title={finiteValue(metric.mean_ms) ? undefined : "Not measured"}>{duration(metric.mean_ms)}<span>average</span></p>
    <dl className="performance-percentiles">
      <div><dt>Median</dt><dd title={finiteValue(metric.p50_ms) ? undefined : "Not measured"}>{duration(metric.p50_ms)}</dd></div>
      <div><dt>p95</dt><dd title={finiteValue(metric.p95_ms) ? undefined : "Not measured"}>{duration(metric.p95_ms)}</dd></div>
      <div><dt>Slowest</dt><dd title={finiteValue(metric.max_ms) ? undefined : "Not measured"}>{duration(metric.max_ms)}</dd></div>
    </dl>
    <p className="performance-coverage">{spendCount(metric.samples)} measured{metric.pending > 0 && <> · {spendCount(metric.pending)} waiting</>}</p>
    {(metric.missing > 0 || Boolean(metric.excluded)) && <p className="performance-coverage">{spendCount(metric.missing)} unavailable{Boolean(metric.excluded) && <> · {spendCount(metric.excluded)} excluded from average</>}</p>}
    {metric.reasons && <details className="performance-coverage"><summary>Timing coverage · {spendCount(metric.total)} {metric.total === 1 ? "turn" : "turns"}</summary>
      <ul>{Object.entries(metric.reasons).filter(([, count]) => count > 0).map(([reason, count]) =>
        <li key={reason}>{spendCount(count)} · {timingReason(reason)}</li>)}</ul>
    </details>}
  </Card>
}

export function StartupPerformance({ data }: { data?: NativePerformance }) {
  if (!data) return <p className="analytics-status" role="status">Performance data is unavailable.</p>
  const { dispatch, breakdown, computer } = data
  return <>
    {data.cards ? <div className="performance-metrics">
      {timingSeries.map(item => <PerformanceCard key={item.key} title={item.title} description={item.description} metric={data.cards![item.key]} id={`performance-${item.key}`} />)}
    </div> : <p className="analytics-status">Startup measurements are unavailable.</p>}
    {!dispatch.samples && <p className="analytics-status" role="status">No complete startup measurements in this range.</p>}
    <PerformanceTrend rows={data.trend} />
    <details className="analytics-breakdown">
      <summary>Startup breakdown</summary>
      <p className="analytics-status">Average across {spendCount(breakdown.samples)} complete measurements.</p>
      <ReportTable label="Startup breakdown" columns={["Stage", "Average"]}>
        <TableRow><TableCell>Queue wait</TableCell><TableCell>{elapsed(breakdown.api_to_worker_ms.mean_ms)}</TableCell></TableRow>
        <TableRow><TableCell>Context and workspace setup</TableCell><TableCell>{elapsed(breakdown.worker_to_broker_ms.mean_ms)}</TableCell></TableRow>
        <TableRow><TableCell>Model request setup</TableCell><TableCell>{elapsed(breakdown.broker_to_dispatch_ms.mean_ms)}</TableCell></TableRow>
        <TableRow><TableCell><strong>Total</strong></TableCell><TableCell><strong>{elapsed(breakdown.api_to_dispatch_ms.mean_ms)}</strong></TableCell></TableRow>
      </ReportTable>
    </details>
    <details className="analytics-breakdown">
      <summary>Computer startup</summary>
      <ReportTable label="Computer readiness by acquisition mode" columns={["Acquisition", "Ready samples", "Mean", "p50", "p95"]}>
        {([["cold", "Cold start"], ["restore", "Snapshot restore"], ["reuse", "Existing computer"]] as const).map(([mode, label]) => {
          const item = computer[mode]
          return <TableRow key={mode}><TableCell>{label}</TableCell><TableCell>{spendCount(item.samples)}</TableCell><TableCell>{elapsed(item.mean_ms)}</TableCell><TableCell>{elapsed(item.p50_ms)}</TableCell><TableCell>{elapsed(item.p95_ms)}</TableCell></TableRow>
        })}
      </ReportTable>
      <p className="analytics-status">{spendCount(computer.requested)} attempts · {spendCount(computer.failed)} failed · {spendCount(computer.pending)} pending</p>
    </details>
  </>
}
