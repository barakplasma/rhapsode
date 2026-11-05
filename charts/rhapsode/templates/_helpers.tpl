{{- define "rhapsode.homerNamespace" -}}
{{ .Values.homerNamespace | default .Release.Namespace }}
{{- end -}}

{{- define "rhapsode.labels" -}}
app.kubernetes.io/name: rhapsode
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
helm.sh/chart: rhapsode-{{ .Chart.Version | replace "+" "_" }}
{{- end -}}

{{- define "rhapsode.image" -}}
image: "{{ .Values.image.repository }}:{{ .Values.image.tag | default .Chart.AppVersion }}"
imagePullPolicy: {{ .Values.image.pullPolicy | default (ternary "Always" "IfNotPresent" (eq (.Values.image.tag | default .Chart.AppVersion) "latest")) }}
{{- end -}}
