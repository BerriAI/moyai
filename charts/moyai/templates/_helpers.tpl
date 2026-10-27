{{- define "moyai.name" -}}
{{- printf "%s-moyai" .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- define "moyai.image" -}}
{{- printf "%s@%s" .Values.image.repository (required "image.digest must pin a Moyai release containing this chart's runtime" .Values.image.digest) -}}
{{- end -}}
{{- define "moyai.path" -}}
{{- trimSuffix "/" ((urlParse .Values.publicUrl).path) -}}
{{- end -}}
{{- define "moyai.account" -}}
{{- default (include "moyai.name" .) .Values.serviceAccount.name -}}
{{- end -}}
{{- define "moyai.env" -}}
envFrom:
  - secretRef: {name: {{ .Values.runtimeSecret | quote }}}
  - configMapRef: {name: {{ include "moyai.name" . }}-config}
env:
  - name: MOYAI_DATABASE_URL
    valueFrom:
      secretKeyRef: {name: {{ .Values.databaseSecret | quote }}, key: MOYAI_DATABASE_URL}
{{- end -}}
{{- define "moyai.mounts" -}}
volumeMounts:
  - {name: state, mountPath: /data}
  - {name: ingress, mountPath: /data/ingress}
{{- end -}}
{{- define "moyai.security" -}}
securityContext:
  allowPrivilegeEscalation: false
  capabilities: {drop: [ALL]}
{{- end -}}
