{{- define "omnivec.pipelineLanes.validate" -}}
{{- $lanes := .Values.azure.serviceBus.pipelineLanes -}}
{{- if or $lanes.producersEnabled $lanes.workerEnabled -}}
{{- if or (empty $lanes.topicName) (eq (lower $lanes.topicName) "embeddings") -}}
{{- fail "Pipeline lanes require an explicit separate marked topicName (embeddings-lanes-v1), never embeddings" -}}
{{- end -}}
{{- if empty .Values.azure.serviceBus.namespace -}}
{{- fail "Pipeline lanes require azure.serviceBus.namespace" -}}
{{- end -}}
{{- if and $lanes.workerEnabled .Values.dotnetWorker.autoscaling.enabled -}}
{{- fail "Stage-1 lane workers require fixed replicas: disable dotnetWorker.autoscaling (legacy subscription KEDA is unsafe)" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "omnivec.pipelineLanes.ingestionEnv" -}}
{{- include "omnivec.pipelineLanes.validate" . -}}
- name: ChangeFeed__PipelineLanesEnabled
  value: {{ .Values.azure.serviceBus.pipelineLanes.producersEnabled | quote }}
- name: ChangeFeed__PipelineLanesTopicName
  value: {{ .Values.azure.serviceBus.pipelineLanes.topicName | quote }}
{{- end -}}
