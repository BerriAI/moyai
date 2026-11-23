export const timingReasons: Record<string, string> = {
  measured: "Measured",
  waiting: "Waiting for this stage",
  steering_input: "Steering input · measured with parent",
  control: "Control notice · no model execution",
  not_instrumented: "No instrumentation record",
  ended_before_preparation: "Ended before preparation completed",
  ended_without_observation: "Ended without this stage recorded",
  missing_start: "Receipt timestamp not recorded",
  missing_telemetry: "Missing timing observation",
  invalid_timestamps: "Invalid or out-of-order timestamps",
  no_acquisition: "No computer acquisition recorded",
  acquisition_failed: "Computer acquisition failed",
  acquisition_incomplete: "Computer acquisition did not complete",
  outside_range: "Turn admitted outside this date range",
}

export function timingReason(status?: string) {
  return status ? timingReasons[status] ?? "Unknown timing status" : ""
}

export function timingSource(source?: string | null) {
  return source === "http" ? "HTTP receipt"
    : source === "admission" ? "Native admission"
      : source === "control" ? "Control notice" : "Not recorded"
}
