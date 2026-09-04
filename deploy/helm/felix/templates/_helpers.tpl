{{- define "felix.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "felix.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name (include "felix.name" .) | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "felix.labels" -}}
app.kubernetes.io/name: {{ include "felix.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "felix.selectorLabels" -}}
app.kubernetes.io/name: {{ include "felix.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "felix.serviceAccountName" -}}
{{- if .Values.serviceAccount.create -}}
{{- default (include "felix.fullname" .) .Values.serviceAccount.name -}}
{{- else -}}
{{- default "default" .Values.serviceAccount.name -}}
{{- end -}}
{{- end -}}

{{- define "felix.secretName" -}}
{{- if .Values.secrets.existingSecret -}}
{{- .Values.secrets.existingSecret -}}
{{- else if and .Values.externalSecrets.enabled .Values.externalSecrets.targetSecretName -}}
{{- .Values.externalSecrets.targetSecretName -}}
{{- else -}}
{{- include "felix.fullname" . -}}
{{- end -}}
{{- end -}}

{{/*
Selector labels for one process: the Service, the PDB and the HPA select the api alone.
*/}}
{{- define "felix.componentSelectorLabels" -}}
{{ include "felix.selectorLabels" .root }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "felix.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag }}
{{- end -}}

{{/*
Environment, in three tiers so each process gets what it reads and nothing more.

  datastoreEnv  every process: the Postgres and Redis URLs.
  agentEnv      api and worker: model and object-store credentials. The worker runs the
                agent loop (fiber resume, continuous eval), so it needs these.
  authEnv       api only: the JWT signing key and the /internal shared secret. Nothing on
                the worker path reads them, and the worker executes tools on model output,
                so a file-read primitive there must not find a token-signing key.

Keys marked optional are absent from the Secret in deployments that do not use them.
*/}}
{{- define "felix.datastoreEnv" -}}
- name: FELIX_DATA_DIR
  value: /data
- name: FELIX_DATABASE_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "felix.secretName" . }}
      key: FELIX_DATABASE_URL
- name: FELIX_REDIS_URL
  valueFrom:
    secretKeyRef:
      name: {{ include "felix.secretName" . }}
      key: FELIX_REDIS_URL
{{- end -}}

{{- define "felix.optionalSecretEnv" -}}
{{- range $key := .keys }}
- name: {{ $key }}
  valueFrom:
    secretKeyRef:
      name: {{ include "felix.secretName" $.root }}
      key: {{ $key }}
      optional: true
{{- end }}
{{- end -}}

{{- define "felix.agentEnv" -}}
{{ include "felix.optionalSecretEnv" (dict "root" . "keys" (list "FELIX_S3_ACCESS_KEY" "FELIX_S3_SECRET_KEY" "FELIX_ANTHROPIC_API_KEY" "FELIX_OPENAI_API_KEY")) }}
- name: FELIX_S3_ENDPOINT
  value: {{ .Values.s3.endpoint | quote }}
- name: FELIX_S3_BUCKET
  value: {{ .Values.s3.bucket | quote }}
- name: FELIX_S3_REGION
  value: {{ .Values.s3.region | quote }}
{{- end -}}

{{- define "felix.authEnv" -}}
{{ include "felix.optionalSecretEnv" (dict "root" . "keys" (list "FELIX_JWKS_PUBLIC" "FELIX_JWKS_PRIVATE" "FELIX_CONSUMER_SHARED_SECRET")) }}
{{- end -}}

{{/*
Container fields every Felix process shares. The runtime image has /app/.venv on PATH,
so commands are the bare console-script names — never `uv run` (uv is builder-only).
*/}}
{{- define "felix.containerBase" -}}
image: {{ include "felix.image" . | quote }}
imagePullPolicy: {{ .Values.image.pullPolicy }}
securityContext:
  {{- toYaml .Values.securityContext | nindent 2 }}
volumeMounts:
  - name: tmp
    mountPath: /tmp
  - name: data
    mountPath: /data
envFrom:
  - configMapRef:
      name: {{ include "felix.fullname" . }}
{{- end -}}

{{/*
Pod-level fields every Felix Deployment shares.
*/}}
{{- define "felix.podSpecCommon" -}}
serviceAccountName: {{ include "felix.serviceAccountName" . }}
{{- with .Values.imagePullSecrets }}
imagePullSecrets:
  {{- toYaml . | nindent 2 }}
{{- end }}
securityContext:
  {{- toYaml .Values.podSecurityContext | nindent 2 }}
volumes:
  - name: tmp
    emptyDir: {}
  - name: data
    {{- if .Values.persistence.enabled }}
    persistentVolumeClaim:
      claimName: {{ .Values.persistence.existingClaim | default (printf "%s-data" (include "felix.fullname" .)) }}
    {{- else }}
    emptyDir: {}
    {{- end }}
{{- end -}}

{{- define "felix.podSpecPlacement" -}}
{{- with .Values.nodeSelector }}
nodeSelector:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations:
  {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
