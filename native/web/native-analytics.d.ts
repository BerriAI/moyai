declare module "*?native-analytics" {
  export type ChartSeries = { key: string; label: string; line?: boolean }
  export type ChartRow = { date: string; partial?: boolean; [key: string]: string | number | boolean | null | undefined }
  export type ChartOptions = { title: string; money?: boolean; area?: boolean; cumulative?: boolean; line?: ChartSeries | null }
  export function analyticsDate(value: string): string
  export function analyticsPreset(value: string, today?: Date): { start: string; end: string } | null
  export function analyticsMetrics(items: [string, string, string?][]): string
  export function analyticsLegend(series: ChartSeries[], cumulative?: boolean): string
  export function analyticsChart(rows: ChartRow[], series: ChartSeries[], options: ChartOptions): string
  export function analyticsCSV(rows: unknown[][]): string
}
