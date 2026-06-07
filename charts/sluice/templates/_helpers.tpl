{{/* Common env for components that select backends via sluice_core.config.Settings */}}
{{- define "sluice.backendEnv" -}}
- { name: QUEUE__BACKEND, value: "{{ .Values.queue.backend }}" }
- { name: QUEUE__OPTIONS, value: {{ .Values.queue.options | toJson | quote }} }
- { name: OBJECT_STORE__BACKEND, value: "{{ .Values.object_store.backend }}" }
- { name: OBJECT_STORE__OPTIONS, value: {{ .Values.object_store.options | toJson | quote }} }
{{- end -}}

{{/* Optional secret with backend credentials (AWS_*, etc.) mounted as env on every component */}}
{{- define "sluice.backendSecretEnvFrom" -}}
{{- if .Values.backendSecret }}
          envFrom:
            - secretRef: { name: "{{ .Values.backendSecret }}" }
{{- end }}
{{- end -}}
