import { TableCell, TableRow } from "@/components/ui/table"
import type { NativePerformance } from "./analytics-data"
import { elapsed, spendCount } from "./analytics-format"
import { AnalyticsMetrics, ReportTable } from "./analytics-shared"

export function StartupPerformance({ data }: { data?: NativePerformance }) {
  if (!data) return <p className="analytics-status" role="status">Startup measurements are unavailable from this server. The native API and execution workers need the performance instrumentation release.</p>
  const { dispatch, breakdown, computer } = data
  return <>
    <section className="analytics-section" aria-labelledby="startup-timing-title">
      <h2 id="startup-timing-title">API receipt → first foreground LiteLLM dispatch</h2>
      <p className="analytics-status">Time Moyai spends getting an accepted message to its first outbound model attempt. Ends after broker admission, before waiting for the gateway response.</p>
      <AnalyticsMetrics items={[
        ["Mean startup", elapsed(dispatch.mean_ms)], ["Median startup (p50)", elapsed(dispatch.p50_ms)],
        ["p95 startup", elapsed(dispatch.p95_ms)],
        ["Measured root turns", `${spendCount(dispatch.samples)} / ${spendCount(data.eligible_turns)}`, `${spendCount(data.missing_dispatch_samples)} without a complete measurement`],
      ]} />
      {!dispatch.samples && <p className="analytics-status" role="status">No complete startup measurements in this range yet. New web messages acquire timings once the updated API, execution worker and broker are running. Older records are not backfilled.</p>}
      <h3>Where Moyai spent the time</h3>
      <p className="analytics-status">Component means use the same {spendCount(breakdown.samples)} complete root turns and add to the total below.</p>
      <ReportTable label="Startup breakdown" columns={["Stage", "Mean", "What it includes"]}>
        <TableRow><TableCell>API → execution worker</TableCell><TableCell>{elapsed(breakdown.api_to_worker_ms.mean_ms)}</TableCell><TableCell>Authentication, admission, queue wait and worker pickup</TableCell></TableRow>
        <TableRow><TableCell>Execution worker → broker</TableCell><TableCell>{elapsed(breakdown.worker_to_broker_ms.mean_ms)}</TableCell><TableCell>Context, workspace preparation when needed, and broker handoff</TableCell></TableRow>
        <TableRow><TableCell>Broker → LiteLLM dispatch</TableCell><TableCell>{elapsed(breakdown.broker_to_dispatch_ms.mean_ms)}</TableCell><TableCell>Authorization, capacity wait and request admission</TableCell></TableRow>
        <TableRow><TableCell><strong>Total for this cohort</strong></TableCell><TableCell><strong>{elapsed(breakdown.api_to_dispatch_ms.mean_ms)}</strong></TableCell><TableCell>{spendCount(breakdown.samples)} complete measurements</TableCell></TableRow>
      </ReportTable>
      <p className="analytics-muted">This measures the outbound attempt, not when LiteLLM receives it. Direct local execution uses local inference admission as the broker boundary.</p>
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
