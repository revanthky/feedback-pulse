{{- define "feedback-pulse.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "feedback-pulse.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "feedback-pulse.labels" -}}
app.kubernetes.io/name: {{ include "feedback-pulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "feedback-pulse.selectorLabels" -}}
app.kubernetes.io/name: {{ include "feedback-pulse.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
