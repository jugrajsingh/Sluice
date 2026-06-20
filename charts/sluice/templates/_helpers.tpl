{{/* Backend selection + non-secret options for components that read sluice_core.config.Settings.
     OPTIONS are emitted only when non-empty, so a capability Secret (below) can supply
     QUEUE__OPTIONS / OBJECT_STORE__OPTIONS (e.g. a Redis URL with password) without colliding. */}}
{{- define "sluice.backendEnv" -}}
- { name: QUEUE__BACKEND, value: "{{ .Values.queue.backend }}" }
{{- if .Values.queue.options }}
- { name: QUEUE__OPTIONS, value: {{ .Values.queue.options | toJson | quote }} }
{{- end }}
- { name: OBJECT_STORE__BACKEND, value: "{{ .Values.object_store.backend }}" }
{{- if .Values.object_store.options }}
- { name: OBJECT_STORE__OPTIONS, value: {{ .Values.object_store.options | toJson | quote }} }
{{- end }}
{{/* STATE_STORE__* only when a separate state backend is configured; empty ⇒ Settings.state_store=None ⇒ inherit object_store (ADR-011). */}}
{{- $st := .Values.state_store | default dict -}}
{{- if $st.backend }}
- { name: STATE_STORE__BACKEND, value: "{{ $st.backend }}" }
{{- if $st.options }}
- { name: STATE_STORE__OPTIONS, value: {{ $st.options | toJson | quote }} }
{{- end }}
{{- end }}
{{- end -}}

{{/* ---- Capability-scoped credentials -------------------------------------------------------
     Each helper takes (dict "root" $ "caps" (list "storage" "queue" ...)) and emits the creds
     for those capabilities. A capability that sets `envFromSecret` is injected as env (AWS_*,
     or QUEUE__/CACHE__OPTIONS); one that sets `file.secret` is mounted at
     /var/secrets/cred-<cap>/<key> with `env` (default GOOGLE_APPLICATION_CREDENTIALS) -> that path.
     Point several capabilities at the same Secret for one all-access identity, or distinct
     Secrets to mix clouds. (Never Workload Identity — externally-provisioned mounted secrets only.) */}}

{{/* `envFrom:` block (10-space indent) — emitted only when ≥1 capability sets envFromSecret. */}}
{{- define "sluice.credEnvFrom" -}}
{{- $root := .root -}}
{{- $names := list -}}
{{- range .caps -}}
  {{- $c := index $root.Values.credentials . -}}
  {{- if and $c $c.envFromSecret -}}{{- $names = append $names $c.envFromSecret -}}{{- end -}}
{{- end -}}
{{- with ($names | uniq) }}
          envFrom:
          {{- range . }}
            - secretRef: { name: "{{ . }}" }
          {{- end }}
{{- end -}}
{{- end -}}

{{/* file-cred env vars (12-space indent, sits inside an existing `env:` list), deduped by env name. */}}
{{- define "sluice.credFileEnv" -}}
{{- $root := .root -}}
{{- $seen := dict -}}
{{- range .caps -}}
  {{- $c := index $root.Values.credentials . -}}
  {{- if and $c $c.file $c.file.secret -}}
    {{- $env := default "GOOGLE_APPLICATION_CREDENTIALS" $c.file.env -}}
    {{- $key := default "credentials.json" $c.file.key -}}
    {{- if not (hasKey $seen $env) -}}{{- $_ := set $seen $env (printf "/var/secrets/cred-%s/%s" . $key) -}}{{- end -}}
  {{- end -}}
{{- end -}}
{{- range $env, $path := $seen }}
            - { name: {{ $env }}, value: {{ $path }} }
{{- end -}}
{{- end -}}

{{/* volumeMounts (12-space indent, inside a `volumeMounts:` list) for file-based capability creds. */}}
{{- define "sluice.credVolumeMounts" -}}
{{- $root := .root -}}
{{- range .caps -}}
  {{- $c := index $root.Values.credentials . -}}
  {{- if and $c $c.file $c.file.secret }}
            - { name: cred-{{ . }}, mountPath: /var/secrets/cred-{{ . }}, readOnly: true }
  {{- end -}}
{{- end -}}
{{- end -}}

{{/* volumes (8-space indent, inside a `volumes:` list) for file-based capability creds. */}}
{{- define "sluice.credVolumes" -}}
{{- $root := .root -}}
{{- range .caps -}}
  {{- $c := index $root.Values.credentials . -}}
  {{- if and $c $c.file $c.file.secret }}
        - name: cred-{{ . }}
          secret: { secretName: "{{ $c.file.secret }}" }
  {{- end -}}
{{- end -}}
{{- end -}}

{{/* imagePullSecrets block (6-space indent, under a pod `spec:`). */}}
{{- define "sluice.imagePullSecrets" -}}
{{- with .Values.imagePullSecrets }}
      imagePullSecrets: {{ toYaml . | nindent 8 }}
{{- end }}
{{- end -}}
