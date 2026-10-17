{{/*
Expand the name of the chart.
*/}}
{{- define "omnivec.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "omnivec.mockSinkUrls" -}}
{{- $count := int .Values.api.mockBenchmark.shardCount -}}
{{- if or (lt $count 1) (gt $count 32) -}}
{{- fail "api.mockBenchmark.shardCount must be between 1 and 32" -}}
{{- end -}}
{{- $urls := list "http://omnivec-mock-sink:8080" -}}
{{- range $shard := until $count -}}
{{- if gt $shard 0 -}}{{- $urls = append $urls (printf "http://omnivec-mock-sink-%d:8080" $shard) -}}{{- end -}}
{{- end -}}
{{- join "," $urls -}}
{{- end -}}

{{- define "omnivec.mockSourceUrls" -}}
{{- $count := int .Values.api.mockBenchmark.shardCount -}}
{{- if or (lt $count 1) (gt $count 32) -}}
{{- fail "api.mockBenchmark.shardCount must be between 1 and 32" -}}
{{- end -}}
{{- $urls := list -}}
{{- range $shard := until $count -}}
{{- $urls = append $urls (printf "http://omnivec-mock-runner-%d:8080" $shard) -}}
{{- end -}}
{{- join "," $urls -}}
{{- end -}}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "omnivec.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "omnivec.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "omnivec.labels" -}}
helm.sh/chart: {{ include "omnivec.chart" . }}
{{ include "omnivec.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "omnivec.selectorLabels" -}}
app.kubernetes.io/name: {{ include "omnivec.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "omnivec.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "omnivec.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Image name with registry
*/}}
{{- define "omnivec.image" -}}
{{- if .Values.global.imageRegistry }}
{{- printf "%s/%s:%s" .Values.global.imageRegistry .repository .tag }}
{{- else }}
{{- printf "%s:%s" .repository .tag }}
{{- end }}
{{- end }}
