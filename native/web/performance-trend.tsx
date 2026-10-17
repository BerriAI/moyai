import { Card } from "@/components/ui/card"
import { TableCell, TableRow } from "@/components/ui/table"
import type { TimingCardKey, TimingInterval } from "./analytics-data"
import { elapsed, finiteValue, spendCount } from "./analytics-format"
import { ReportTable } from "./analytics-shared"

export const timingSeries: { key: TimingCardKey; title: string; description: string }[] = [
  { key: "startup", title: "Startup", description: "Time until the first model request is sent" },
  { key: "queue", title: "Queue wait", description: "Time until work starts" },
  { key: "broker", title: "Request setup", description: "Time spent preparing the model request" },
  { key: "computer", title: "Computer startup", description: "Time until the computer is ready" },
]

const intervalTime = (value: string) => new Date(value).toLocaleString(undefined, { timeZone: "UTC" })

/** Split at unobserved bins: no invented zeros or lines crossing data gaps. */
export function trendSegments(values: (number | null)[]): number[][] {
  const result: number[][] = []
  let segment: number[] = []
  values.forEach((value, index) => {
    if (finiteValue(value)) segment.push(index)
    else if (segment.length) { result.push(segment); segment = [] }
  })
  if (segment.length) result.push(segment)
  return result
}

export function PerformanceTrend({ rows }: { rows?: TimingInterval[] }) {
  if (!rows?.length) return null
  const values = rows.flatMap(row => timingSeries.map(({ key }) => row[key].mean_ms)).filter(finiteValue)
  const maximum = Math.max(10, ...values)
  const x = (index: number) => 60 + (index + 0.5) * 764 / rows.length
  const y = (value: number) => 174 - value / maximum * 156
  return <Card className="performance-trend" role="region" aria-labelledby="performance-trend-title">
    <div className="performance-section-heading"><h2 id="performance-trend-title">Average latency over time</h2><span>UTC</span></div>
    <div className="performance-legend">{timingSeries.map(item => <span key={item.key} className={`performance-series-${item.key}`}><i aria-hidden="true" />{item.title}</span>)}</div>
    {values.length ? <div className="performance-chart-scroll" role="region" aria-label="Latency trend chart" tabIndex={0}>
      <svg viewBox="0 0 840 200" role="img" aria-label="Average startup, queue pickup, broker preparation and computer readiness. Exact values are available below.">
        {[0, maximum / 2, maximum].map(value => <g key={value}>
          <line className="performance-gridline" x1="60" y1={y(value)} x2="824" y2={y(value)} />
          <text className="performance-axis-label" x="50" y={y(value) + 4} textAnchor="end">{elapsed(value)}</text>
        </g>)}
        {timingSeries.map(({ key, title }) => {
          const values = rows.map(row => row[key].mean_ms)
          return <g key={key} className={`performance-series-${key}`}>
            {trendSegments(values).filter(segment => segment.length > 1).map(segment => <polyline
              key={segment[0]} className="performance-line"
              points={segment.map(index => `${x(index)},${y(values[index]!)}`).join(" ")}
            />)}
            {values.map((value, index) => finiteValue(value) && <circle key={index} className="performance-point" cx={x(index)} cy={y(value)} r="3.5">
              <title>{title}: {elapsed(value)} · {rows[index][key].samples} measured · {intervalTime(rows[index].start)} UTC</title>
            </circle>)}
          </g>
        })}
      </svg>
    </div> : <p className="analytics-status">No measurements in this range.</p>}
    <div className="performance-chart-range"><span>{intervalTime(rows[0].start)} UTC</span><span>{intervalTime(rows[rows.length - 1].end)} UTC</span></div>
    <details className="performance-intervals"><summary>View interval values</summary>
      <ReportTable label="Latency trend interval values" columns={["Interval start (UTC)", ...timingSeries.map(item => item.title)]}>
        {rows.map(row => <TableRow key={row.start}><TableCell>{intervalTime(row.start)}</TableCell>{timingSeries.map(({ key }) => <TableCell key={key}>{elapsed(row[key].mean_ms)}<small>{spendCount(row[key].samples)} measured</small></TableCell>)}</TableRow>)}
      </ReportTable>
    </details>
  </Card>
}
