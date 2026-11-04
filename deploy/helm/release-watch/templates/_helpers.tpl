{{- define "rw.labels" -}}
app.kubernetes.io/name: release-watch
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end -}}

{{- define "rw.env" -}}
- name: POSTGRES_PASSWORD
  valueFrom: { secretKeyRef: { name: {{ .Values.existingSecret }}, key: POSTGRES_PASSWORD } }
- name: RW_DATABASE_URL
  value: "postgresql+psycopg://{{ .Values.postgres.user }}:$(POSTGRES_PASSWORD)@{{ .Release.Name }}-postgres:5432/{{ .Values.postgres.database }}"
{{- range $key := list "RW_DOCEXTRACTOR_API_KEY" "RW_AZURE_OPENAI_API_KEY" "RW_AZURE_OPENAI_ENDPOINT" "RW_OPENROUTER_API_KEY" "RW_TEAMS_WEBHOOK_URL" "RW_BROWSERLESS_TOKEN" }}
- name: {{ $key }}
  valueFrom: { secretKeyRef: { name: {{ $.Values.existingSecret }}, key: {{ $key }}, optional: true } }
{{- end }}
{{- end -}}
